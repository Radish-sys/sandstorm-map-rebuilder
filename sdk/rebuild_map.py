"""
Sandstorm Map Rebuilder - SDK side.

Rebuilds a map extracted by extract_map.py inside the Insurgency: Sandstorm SDK:
the mod's custom static meshes (from meshes/*.fbx), textures, material instances and stand-ins
for custom materials, then every level and sublevel, actors, volumes, foliage, lighting,
level streaming, and (best effort) scenarios + World Settings.

Run it from the editor's SMR menu (installed by the Sandstorm Map Rebuilder app), or with
File > Execute Python Script... from inside an extracted map folder (next to layout.json, textures/ and meshes/).
Arguments: --folder <extracted map folder>, and 'landscape' to only rebuild the landscape material.

Safe to re-run: actors it spawned before (tagged SMR_Rebuild) are deleted first, and
assets that already exist are left alone. A log is written to rebuild_log.txt.
The landscape is the one thing you import by hand; the log tells you the exact settings.
"""
import json
import os
import re
import sys
import unreal

# ------------------------------------------------------------------ config
# Content root of the mod you created in the SDK. None = the name the app chose (layout.json target_mod),
# or the original's name for extracts made before r28. Every path is remapped into it.
NEW_MOD_ROOT = None
# Only rebuild these levels (short names, e.g. ['Rust_FFA']). None = everything.
ONLY_LEVELS = None
# Steps that edit assets other than levels. Turn off any you'd rather do by hand.
DO_TEXTURES_AND_MATERIALS = True
DO_MESHES = True
DO_BLUEPRINTS = True
DO_SCENARIOS = True
DO_WORLD_SETTINGS = True



def _work_folder():
    """The extracted map folder: --folder <path> (the SMR editor menu passes it), else this script's folder."""
    a = sys.argv[1:]
    if '--folder' in a and a.index('--folder') + 1 < len(a):
        return os.path.abspath(a[a.index('--folder') + 1])
    return os.path.dirname(os.path.abspath(__file__))


HERE = _work_folder()
LAYOUT = os.path.join(HERE, 'layout.json')
TAG = 'SMR_Rebuild'
BUILD = 'r40'  # must match smr/__init__.py __build__; logged so the rebuild_log says which script ran
SKIP_PROPS = {'SpawnCollisionHandlingMethod', 'BrushType', 'UCSModifiedProperties', 'OldPosition',
              'bOldPositionValid', 'ChildActor', 'ModularMeshComponent', 'TopMeshComponent',
              'BottomMeshComponent', 'BoxComponent', 'TopBoxComponent', 'CameraComponent', 'SceneComponent',
              'LightComponent', 'SkyLightComponent', 'Component', 'FogComponent', 'Settings_Deprecated',
              'BodyInstance', 'ParentComponent'}
STD_CHANNELS = {'WorldStatic': 'WORLD_STATIC', 'WorldDynamic': 'WORLD_DYNAMIC', 'Pawn': 'PAWN',
                'Visibility': 'VISIBILITY', 'Camera': 'CAMERA', 'PhysicsBody': 'PHYSICS_BODY',
                'Vehicle': 'VEHICLE', 'Destructible': 'DESTRUCTIBLE'}
LIGHT_CLASSES = ('DirectionalLight', 'SkyLight')

EL = unreal.EditorLevelLibrary
EAL = unreal.EditorAssetLibrary
log_lines = []
manual = []  # things the user has to do by hand, shown at the end
MISSING_ASSETS = []  # asset paths nothing could be found for
FOLIAGE_LEFT = []  # [level, mesh, instances not placed, total] when instanced foliage isn't available
FULL_RUN = {'on': False}  # TODO.md is only written by a full rebuild
stats = {'spawned': 0, 'failed': 0, 'prop_fail': 0}
OLD_ROOT = None


def log(msg):
    log_lines.append(msg)
    unreal.log_warning('[SMR] ' + msg)


def todo(msg):
    manual.append(msg)
    log('TODO: ' + msg)


RELOCATE = {}  # old /Game package -> package inside the mod (filled from layout.json)
FALLBACK = {}  # /Game package -> package inside the mod, used only if the SDK doesn't have the original
DEP_MODS = []  # other mod roots the old pak carried; they move under <mod>/Deps/<name>
LAYOUT_DATA = {}
RENAMES = {}  # old level/scenario package -> new one (layout.json renames): the map gets a name the original lacks
SHORT_RENAMES = {}  # the same by short name (scenario sublevel lists and Map fields hold names, not paths)


def _renamed(path):
    """/JunkYard/Maps/JunkYard.JunkYard:PersistentLevel.X -> /JunkYard/Maps/JunkYardRPK.JunkYardRPK:PersistentLevel.X"""
    pk = path.split('.')[0].split(':')[0]
    new = RENAMES.get(pk)
    if not new:
        return path
    rest = path[len(pk):]
    o, n = pk.rsplit('/', 1)[-1], new.rsplit('/', 1)[-1]
    if rest.startswith('.' + o) and rest[len(o) + 1:len(o) + 2] in ('', ':', '.'):
        rest = '.' + n + rest[len(o) + 1:]
    return new + rest


def menu_name():
    """The map's entry in the SMR menu. The app lists a map by its paks' name (smr/core.py map_name), which is not
    the rebuilt map's name (Panji_port, not P4njRPK), so instructions must use this one."""
    paks = LAYOUT_DATA.get('source_paks') or []
    if not paks:
        return 'this map'
    n = os.path.splitext(os.path.basename(paks[0]))[0]
    return re.split(r'pakchunk', n, flags=re.I)[0] or n


def renamed_short(name):
    """A level or scenario short name as the rebuilt mod has it."""
    return SHORT_RENAMES.get(name, name) if isinstance(name, str) else name


def remap(path):
    """Rewrite /OldMod/... to the new mod root, and relocated /Game assets into the mod."""
    if isinstance(path, str) and path.startswith('/Game/'):
        pk = path.split('.')[0]
        if pk in RELOCATE:
            path = RELOCATE[pk] + path[len(pk):]
    if isinstance(path, str):
        for dm in DEP_MODS:
            if path == '/' + dm or path.startswith('/' + dm + '/') or path.startswith('/' + dm + '.'):
                return mod_root() + '/Deps/' + dm + path[len(dm) + 1:]
        if RENAMES:
            path = _renamed(path)
    if isinstance(path, str) and NEW_MOD_ROOT and OLD_ROOT and (path == OLD_ROOT or path.startswith(OLD_ROOT + '/')):
        return NEW_MOD_ROOT + path[len(OLD_ROOT):]
    return path


def mod_root():
    return NEW_MOD_ROOT or OLD_ROOT


def _mod_exists(root):
    """Whether the SDK has a mod (plugin) mounted at /<root>. False only when that is certain."""
    try:
        if EAL.does_directory_exist(root) or EAL.does_directory_exist(root + '/'):
            return True
        return bool(EAL.list_assets(root, False, True))
    except Exception:
        return True  # can't tell: don't block the rebuild on it


def short(path):
    return path.split('/')[-1].split('.')[0]


# ------------------------------------------------------------------ asset lookup with name fallback
_registry_index = {}


def _index(class_name):
    if class_name not in _registry_index:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        idx = {}
        # only this mod and the base game: another mod in the same SDK can have assets with the same names
        # (two maps using the same marketplace pack), and pointing at those would cross-link the mods
        roots = ('/Game/', '/Engine/', (mod_root() or '/__none__') + '/')
        for ad in ar.get_assets_by_class(class_name, True):
            op = str(ad.object_path)
            if op.startswith(roots):
                idx.setdefault(str(ad.asset_name), op)
        _registry_index[class_name] = idx
    return _registry_index[class_name]


_asset_cache = {}


def load_asset(path, class_hint=None):
    if not path:
        return None
    path = remap(path)
    if path in _asset_cache:
        return _asset_cache[path]
    obj = unreal.load_object(None, path)
    if obj is None and path.split('.')[0] in FALLBACK:
        obj = build_fallback(path.split('.')[0])
    if obj is None:
        name = path.split('.')[-1]
        for cls in ([class_hint] if class_hint else []) + ['StaticMesh', 'MaterialInstanceConstant', 'Material',
                                                            'ParticleSystem', 'Texture2D', 'SoundCue', 'World']:
            alt = _index(cls).get(name)
            if alt:
                obj = unreal.load_object(None, alt)
                log('asset moved: %s -> %s' % (path, alt))
                break
    if obj is None:
        if path.split('.')[0] not in MISSING_ASSETS:
            MISSING_ASSETS.append(path.split('.')[0])
        err = (LAYOUT_DATA.get('game_mesh_errors') or {}).get(path.split('.')[0])
        log('MISSING asset: ' + path + (' (its geometry is not in the old paks either: %s)' % err if err else ''))
    _asset_cache[path] = obj
    return obj


_class_cache = {}


def blueprint_class(bp_path):
    """The generated class of a Blueprint asset ('/Game/X/BP_A' or '/Game/X/BP_A.BP_A').
    Blueprint.generated_class() is UE5-only; 4.27 has EditorAssetLibrary.load_blueprint_class."""
    pkg = bp_path.split('.')[0]
    for get in (lambda: EAL.load_blueprint_class(pkg),
                lambda: unreal.load_class(None, '%s.%s_C' % (pkg, short(pkg))),
                lambda: unreal.load_asset(pkg).generated_class()):
        try:
            cls = get()
            if cls:
                return cls
        except Exception:
            pass
    return None


def load_class(path):
    path = remap(path)
    if path in _class_cache:
        return _class_cache[path]
    sname = path.split('.')[-1]
    cls = None
    try:
        cls = unreal.load_class(None, path)
    except Exception:
        cls = None
    if cls is None and sname.endswith('_C'):
        alt = _index('Blueprint').get(sname[:-2])
        if alt:
            cls = blueprint_class(alt)
            if cls:
                log('class moved: %s -> %s' % (path, alt))
    _class_cache[path] = cls
    return cls


# ------------------------------------------------------------------ property application
def py_name(n):
    if len(n) > 1 and n[0] == 'b' and n[1].isupper():
        n = n[1:]
    out = ''
    for i, ch in enumerate(n):
        if ch.isupper() and i > 0 and (n[i - 1].islower() or n[i - 1].isdigit() or
                                       (i + 1 < len(n) and n[i + 1].islower() and n[i - 1] != '_')):
            out += '_'
        out += ch.lower()
    while '__' in out:
        out = out.replace('__', '_')
    return out


deferred_refs = []  # (obj, prop_name, actor_name)
cross_refs = []  # (owner level, owner actor label, owner component name or None, prop, target label, target level)
CUR = {'level': None, 'actor': None}


def assign(obj, pname, val):
    """set_editor_property, also for TSet/TArray properties given a plain list (e.g. an objective's capture zones)."""
    try:
        obj.set_editor_property(pname, val)
        return
    except Exception:
        if not isinstance(val, list):
            raise
    cur = obj.get_editor_property(pname)  # a container of the property's own element type
    cur.clear()
    for x in val:
        (cur.add if hasattr(cur, 'add') else cur.append)(x)
    obj.set_editor_property(pname, cur)


def coerce(cur, v, owner, pname):
    """Convert extracted JSON value v to the type of the current value cur. Returns (ok, value)."""
    if isinstance(v, list) and v and all(isinstance(x, dict) and 'actor_ref' in x for x in v):
        # a list/set of actors (capture zones, spawn zones...): set once the actors exist
        lvls = {remap(x['level']) for x in v if x.get('level')}
        names = [x['actor_ref'] for x in v]
        if lvls and lvls != {CUR['level']}:
            comp = None if isinstance(owner, unreal.Actor) else (
                owner.get_name() if isinstance(owner, unreal.ActorComponent) else False)
            if comp is False or len(lvls) > 1 or any(not x.get('level') for x in v):
                todo('%s.%s should point at %s (set it by hand)' % (CUR['actor'], pname, ', '.join(names)))
            else:
                cross_refs.append((CUR['level'], CUR['actor'], comp, pname, names, lvls.pop()))
            return False, None
        deferred_refs.append((owner, pname, names))
        return False, None
    if isinstance(v, dict) and 'actor_ref' in v:
        if v.get('level') and remap(v['level']) != CUR['level']:
            # an actor in another sublevel: set once all levels exist (end of the rebuild)
            comp = None if isinstance(owner, unreal.Actor) else (
                owner.get_name() if isinstance(owner, unreal.ActorComponent) else False)
            if comp is False:
                todo('%s.%s should point at %s in %s (inside a struct; set it by hand)' % (
                    CUR['actor'], pname, v['actor_ref'], short(v['level'])))
            else:
                cross_refs.append((CUR['level'], CUR['actor'], comp, pname, v['actor_ref'], remap(v['level'])))
            return False, None
        deferred_refs.append((owner, pname, v['actor_ref']))
        return False, None
    num = isinstance(v, (int, float, bool))
    if isinstance(cur, bool):
        return num, bool(v) if num else None
    if isinstance(cur, int) and not isinstance(cur, unreal.EnumBase):
        return num, int(v) if num else None
    if isinstance(cur, float):
        return num, float(v) if num else None
    if isinstance(cur, unreal.EnumBase) and isinstance(v, str):
        s = v.split('::')[-1]
        for cand in (py_name(s).upper(), s.upper(), s):
            if hasattr(type(cur), cand):
                return True, getattr(type(cur), cand)
        return False, None
    if isinstance(cur, unreal.Vector) and isinstance(v, list):
        return True, unreal.Vector(*v[:3])
    if hasattr(unreal, 'Vector4') and isinstance(cur, unreal.Vector4) and isinstance(v, list):
        return True, unreal.Vector4(*(list(v[:4]) + [1.0] * (4 - len(v[:4]))))  # post-process colour grading
    if isinstance(cur, unreal.Vector2D) and isinstance(v, list):
        return True, unreal.Vector2D(*v[:2])
    if isinstance(cur, unreal.Rotator) and isinstance(v, dict):
        return True, unreal.Rotator(roll=v.get('roll', 0), pitch=v.get('pitch', 0), yaw=v.get('yaw', 0))
    if isinstance(cur, unreal.LinearColor) and isinstance(v, list):
        return True, unreal.LinearColor(*v[:4])
    if isinstance(cur, unreal.Color) and isinstance(v, list):
        return True, unreal.Color(r=v[0], g=v[1], b=v[2], a=v[3])
    if isinstance(cur, unreal.StructBase) and isinstance(v, (int, float)) and not isinstance(v, bool):
        # PerPlatformFloat/Int (grass density, cull distances...): older cooks store a plain number
        try:
            cur.set_editor_property('default', type(cur.get_editor_property('default'))(v))
            return True, cur
        except Exception:
            return False, None
    if isinstance(cur, unreal.StructBase) and isinstance(v, dict):
        apply_props(cur, v)
        return True, cur
    if isinstance(cur, unreal.Text) and isinstance(v, str):
        return True, unreal.Text(v)
    if isinstance(cur, unreal.Name) and isinstance(v, str):
        return True, unreal.Name(v)
    if isinstance(cur, str) and isinstance(v, str):
        return True, v
    if (cur is None or isinstance(cur, unreal.Object)) and (v is None or isinstance(v, str)):
        if v is None:
            return True, None
        if v.startswith('/Script/'):
            return True, unreal.load_class(None, v) or unreal.load_object(None, v)
        if v.startswith('/'):
            obj = load_asset(v)
            return obj is not None, obj
        return False, None
    if isinstance(cur, unreal.Array) and isinstance(v, list):
        if all(isinstance(x, str) and x.startswith('/') for x in v):
            return True, [load_asset(x) for x in v]
        if all(isinstance(x, (int, float, bool)) for x in v):
            return True, v
        return False, None
    return False, None


def apply_props(obj, props, skip=()):
    if not props:
        return
    for k, v in props.items():
        if k in SKIP_PROPS or k in skip or '[' in k:
            continue
        name = None
        cur = None
        for cand in (py_name(k), k):
            try:
                cur = obj.get_editor_property(cand)
                name = cand
                break
            except Exception:
                continue
        if name is None:
            stats['prop_fail'] += 1
            continue
        ok, val = coerce(cur, v, obj, name)
        if not ok:
            if not (isinstance(v, dict) and 'actor_ref' in v):
                stats['prop_fail'] += 1
            continue
        try:
            obj.set_editor_property(name, val)
        except Exception as ex:
            if 'read-only' in str(ex) or 'cannot be edited on instances' in str(ex):
                continue  # runtime/derived value (e.g. CineCamera CurrentFocusDistance); nothing to restore
            stats['prop_fail'] += 1
            log('prop %s.%s: %s' % (obj.get_name(), name, ex))


# ------------------------------------------------------------------ spawning
def rot(r):
    return unreal.Rotator(roll=r.get('roll', 0), pitch=r.get('pitch', 0), yaw=r.get('yaw', 0))


def spawn(cls, loc, r, scale, label, folder):
    try:
        a = EL.spawn_actor_from_class(cls, unreal.Vector(*loc), rot(r))
    except TypeError:
        # not an actor class (e.g. a BSP ModelComponent listed by an older extract); nothing to place
        log('skipped %s: %s is not an actor class' % (label, cls.get_name() if hasattr(cls, 'get_name') else cls))
        return None
    if a is None:
        return None
    a.set_actor_scale3d(unreal.Vector(*scale))
    a.set_actor_label(label)
    a.set_folder_path(folder)
    a.set_editor_property('tags', list(a.get_editor_property('tags')) + [TAG])
    stats['spawned'] += 1
    return a


def try_build_cube_brush(actor):
    """Volumes spawned from Python can come without brush geometry; build a 200uu cube."""
    _, ext = actor.get_actor_bounds(False)
    if ext.x > 1 or ext.y > 1:
        return True
    try:
        b = unreal.CubeBuilder()
        b.set_editor_property('x', 200.0)
        b.set_editor_property('y', 200.0)
        b.set_editor_property('z', 200.0)
        b.build(EL.get_editor_world(), actor)
        _, ext = actor.get_actor_bounds(False)
        return ext.x > 1 or ext.y > 1
    except Exception:
        return False


def apply_body_instance(comp, bi):
    """Collision overrides. Custom game channels (Gib, Usable...) can't be set by name from Python."""
    if not bi:
        return
    try:
        if bi.get('CollisionProfileName') and bi['CollisionProfileName'] != 'Custom':
            comp.set_collision_profile_name(bi['CollisionProfileName'])
        if bi.get('CollisionEnabled'):
            s = bi['CollisionEnabled'].split('::')[-1]
            comp.set_collision_enabled(getattr(unreal.CollisionEnabled, py_name(s).upper()))
        skipped = []
        for r in (bi.get('CollisionResponses') or {}).get('ResponseArray', []):
            ch = STD_CHANNELS.get(r.get('Channel'))
            if ch is None and r.get('Channel') and hasattr(unreal.CollisionChannel, 'ECC_' + py_name(r['Channel']).upper()):
                ch = py_name(r['Channel']).upper()  # game channels (Projectile, Gib, Usable...) are exposed by name
            resp = {'ECR_Ignore': unreal.CollisionResponseType.ECR_IGNORE,
                    'ECR_Overlap': unreal.CollisionResponseType.ECR_OVERLAP,
                    'ECR_Block': unreal.CollisionResponseType.ECR_BLOCK}.get(r.get('Response'))
            if ch and resp is not None:
                comp.set_collision_response_to_channel(getattr(unreal.CollisionChannel, 'ECC_' + ch), resp)
            else:
                skipped.append('%s=%s' % (r.get('Channel'), r.get('Response')))
        if skipped:
            log('collision on %s: set by hand -> %s' % (comp.get_owner().get_actor_label(), ', '.join(skipped)))
    except Exception as ex:
        log('collision on %s: %s' % (comp.get_name(), ex))


def components_by_name(actor):
    return {c.get_name(): c for c in actor.get_components_by_class(unreal.ActorComponent)}


missing_classes = {}


def build_actor(a, spawned):
    CUR['actor'] = a['name']
    if a['class'].endswith('.TacticalGrid'):
        # its cover data isn't carried over, and an empty or partial grid makes every bot path query search
        # the whole map (bots freeze the game when the round starts); bots path fine without one
        todo('TacticalGrid not placed (bots work without it). Optional: place one in %s, set Context Generation '
             'Extent to cover the whole map, then Generate Data and Generate Tactical Info' % short(CUR['level'] or ''))
        return
    if a['class'].endswith('.LODActor'):
        return  # HLOD cluster from an older extract: a build product; Build > Build HLODs regenerates them
    if a['class'] in JUMP_PADS:
        act = build_jump_pad(a, JUMP_PADS[a['class']])
        if act:
            spawned[a['name']] = act
            return
    cls = load_class(a['class'])
    if cls is None:
        comps = a.get('components') or {}
        if any((c.get('class') or '').endswith('ChildActorComponent') for c in comps.values()):
            # a BP that only spawned child actors (e.g. kit selection rooms); the children are placed directly
            log('skipped %s (%s): its child actors are placed on their own' % (a['name'], short(a['class'])))
            return
        missing_classes.setdefault(remap(a['class']), []).append(a)
        stats['failed'] += 1
        return
    sname = a['class'].split('.')[-1]
    if 'volume' in a:
        t = a['volume']
        act = spawn(cls, t['location'], t['rotation'], t['scale'], a['name'],
                    'Gameplay' if 'Insurgency' in a['class'] else 'Volumes')
        if act and not try_build_cube_brush(act):
            todo('volume %s has no brush: give it a box brush (its transform is already right)' % a['name'])
        if act and (a.get('brush_box') or {}).get('not_a_box'):
            log('volume %s was not a box in the original; rebuilt as its bounding box' % a['name'])
    else:
        folder = {'StaticMeshActor': 'Meshes', 'INSPlayerStart': 'Gameplay/PlayerStarts'}.get(sname, 'Actors')
        act = spawn(cls, a['location'], a['rotation'], a['scale'], a['name'], folder)
    if act is None:
        stats['failed'] += 1
        log('spawn failed: %s (%s)' % (a['name'], a['class']))
        return
    spawned[a['name']] = act
    apply_props(act, a.get('props'))
    if sname == 'StaticMeshActor':
        smc = act.static_mesh_component
        mesh = load_asset(a.get('mesh'), 'StaticMesh')
        if mesh:
            smc.set_static_mesh(mesh)
        else:
            act.set_actor_label(a['name'] + '__MISSING_MESH')
        for i, m in enumerate(a.get('materials') or []):
            if m:
                mat = load_asset(m)
                if mat:
                    smc.set_material(i, mat)
    comps = components_by_name(act)
    for cname, c in (a.get('components') or {}).items():
        comp = comps.get(cname)
        if comp is None:
            continue
        apply_props(comp, c.get('props'), skip=('StaticMesh', 'OverrideMaterials'))
        if isinstance(comp, unreal.PrimitiveComponent):
            apply_body_instance(comp, (c.get('props') or {}).get('BodyInstance'))


def foliage_type_for(f, mesh):
    """A FoliageType asset for this mesh + settings (FT_<mesh>_<hash> under /<mod>/SMR/Foliage)."""
    import hashlib
    key = json.dumps([f.get('mesh'), f.get('materials'), f.get('cast_shadow', True), f.get('cull_start'),
                      f.get('cull_end'), f.get('collision_profile')], sort_keys=True)
    name = 'FT_%s_%s' % (short(f['mesh'].split('.')[0]), hashlib.md5(key.encode()).hexdigest()[:6])
    path = mod_root() + '/SMR/Foliage/' + name
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    ft = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        name, path.rsplit('/', 1)[0], unreal.FoliageType_InstancedStaticMesh, None)
    if ft is None:
        return None
    _set(ft, 'mesh', mesh)
    mats = [load_asset(m) if m else None for m in (f.get('materials') or [])]
    if any(mats):
        _set(ft, 'override_materials', mats)
    _set(ft, 'cast_shadow', bool(f.get('cast_shadow', True)))
    if f.get('cull_end'):
        try:
            cd = ft.get_editor_property('cull_distance')
            cd.set_editor_property('min', int(f.get('cull_start') or 0))
            cd.set_editor_property('max', int(f['cull_end']))
            ft.set_editor_property('cull_distance', cd)
        except Exception:
            pass
    if f.get('collision_profile'):
        try:
            bi = ft.get_editor_property('body_instance')
            bi.set_editor_property('collision_profile_name', f['collision_profile'])
            ft.set_editor_property('body_instance', bi)
        except Exception:
            pass
    EAL.save_loaded_asset(ft)
    return ft


FOLIAGE_ACTOR_CAP = 1500  # per level, when instanced foliage isn't available: more actors than this runs out of memory
_foliage_actors = {'n': 0}


HOLDER_BP = 'BP_SMR_Instances'  # an Actor Blueprint with one Hierarchical Instanced Static Mesh component


def holder_path():
    return mod_root() + '/SMR/' + HOLDER_BP


def holder_class():
    """The mod's own copy of BP_SMR_Instances. It has to be inside the mod or the cooked map has no foliage,
    so one made anywhere else (another mod, /Game) is copied in the first time."""
    if 'holder' in _class_cache:
        return _class_cache['holder']
    dest = holder_path()
    if not EAL.does_asset_exist(dest):
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        src = [str(ad.object_path).split('.')[0] for ad in ar.get_assets_by_class('Blueprint', True)
               if str(ad.asset_name) == HOLDER_BP]
        if src and EAL.duplicate_asset(src[0], dest):
            EAL.save_asset(dest)
            log('copied %s into the mod: %s' % (src[0], dest))
    _class_cache['holder'] = blueprint_class(dest) if EAL.does_asset_exist(dest) else None
    return _class_cache['holder']


def build_instanced(f, idx, mesh):
    """All of one foliage type's instances in one HISM component on a BP_SMR_Instances actor.
    This SDK's Python can't add to the InstancedFoliageActor, but a HISM component in a placed actor renders,
    saves and cooks the same way, at the same cost."""
    cls = holder_class()
    if cls is None:
        return False
    name = f['mesh'].split('.')[-1]
    act = spawn(cls, (0, 0, 0), {}, (1, 1, 1), 'Foliage_%s_%d' % (name, idx), 'Foliage')
    if act is None:
        return False
    stats['spawned'] -= 1  # counted per instance below
    HISM = unreal.HierarchicalInstancedStaticMeshComponent
    if act.get_component_by_class(HISM) is None:
        log('%s has no Hierarchical Instanced Static Mesh component; add one to it' % holder_path())
        EL.destroy_actor(act)
        _class_cache['holder'] = None
        return False
    # Only setter functions from here on. set_editor_property on a Blueprint actor's component re-runs the
    # construction script, which replaces the component: everything after it would go into the old, discarded
    # one (r26/r27 left every holder empty that way). Fetch the component again after the mobility change.
    for c in (act.get_editor_property('root_component'), act.get_component_by_class(HISM)):
        try:
            c.set_mobility(unreal.ComponentMobility.STATIC)
        except Exception:
            pass
    comp = act.get_component_by_class(HISM)
    comp.set_static_mesh(mesh)
    for mi, m in enumerate(f.get('materials') or []):
        mat = load_asset(m) if m else None
        if mat:
            comp.set_material(mi, mat)
    comp.set_cast_shadow(bool(f.get('cast_shadow', True)))
    if f.get('collision_profile'):
        comp.set_collision_profile_name(f['collision_profile'])
    if f.get('collision_profile') not in (None, 'BlockAll', 'BlockAllDynamic'):
        # grass/gravel: bots walk through it, so it mustn't carve the navmesh (and it makes the build far lighter)
        try:
            comp.set_can_ever_affect_navigation(False)
        except Exception:
            pass
    if f.get('cull_end'):
        comp.set_cull_distances(int(f.get('cull_start') or 0), int(f['cull_end']))
    xf = [unreal.Transform(location=unreal.Vector(*t['location']), rotation=rot(t['rotation']),
                           scale=unreal.Vector(*t['scale'])) for t in f['instances']]
    comp = act.get_component_by_class(HISM)
    try:
        comp.add_instances(xf, False)
    except TypeError:
        comp.add_instances(xf, False, True)  # newer signature has a world-space flag
    comp = act.get_component_by_class(HISM)
    if comp.get_instance_count() != len(xf) or comp.static_mesh != mesh:
        log('foliage %s: holder has %d of %d instances, mesh %s' % (
            name, comp.get_instance_count(), len(xf), comp.static_mesh))
    stats['spawned'] += len(xf)
    return True


def build_foliage(f, idx):
    mesh = load_asset(f['mesh'], 'StaticMesh')
    if mesh is None or not f.get('instances'):
        return
    try:
        if build_instanced(f, idx, mesh):
            return
    except Exception as ex:
        log('instanced foliage for %s failed (%s); placing actors instead' % (short(f['mesh']), ex))
    IFA = getattr(unreal, 'InstancedFoliageActor', None)
    if IFA is not None and hasattr(IFA, 'add_instances'):
        # the original was instanced foliage: thousands of instances in one component, not one actor each
        ft = foliage_type_for(f, mesh)
        if ft is not None:
            world = EL.get_editor_world()
            try:
                if hasattr(IFA, 'remove_all_instances'):
                    IFA.remove_all_instances(world, ft)  # a re-run replaces what the last one painted
                xf = [unreal.Transform(location=unreal.Vector(*t['location']), rotation=rot(t['rotation']),
                                       scale=unreal.Vector(*t['scale'])) for t in f['instances']]
                IFA.add_instances(world, ft, xf)
                stats['spawned'] += len(xf)
                return
            except Exception as ex:
                log('instanced foliage for %s failed (%s); placing actors instead' % (short(f['mesh']), ex))
    # fallback: one StaticMeshActor per instance, capped so a big map can't run the editor out of memory
    name = f['mesh'].split('.')[-1]
    left = FOLIAGE_ACTOR_CAP - _foliage_actors['n']
    if left <= 0:
        FOLIAGE_LEFT.append([short(CUR['level'] or ''), name, len(f['instances']), len(f['instances'])])
        return
    if len(f['instances']) > left:
        FOLIAGE_LEFT.append([short(CUR['level'] or ''), name, len(f['instances']) - left, len(f['instances'])])
    profile = f.get('collision_profile')
    for i, t in enumerate(f['instances'][:left]):
        act = spawn(unreal.StaticMeshActor, t['location'], t['rotation'], t['scale'],
                    '%s_%d_%d' % (name, idx, i), 'Foliage/' + name)
        if act is None:
            continue
        _foliage_actors['n'] += 1
        smc = act.static_mesh_component
        smc.set_static_mesh(mesh)
        for mi, m in enumerate(f.get('materials') or []):
            if m:
                mat = load_asset(m)
                if mat:
                    smc.set_material(mi, mat)
        smc.set_editor_property('cast_shadow', bool(f.get('cast_shadow', True)))
        if profile:
            # keep the original foliage collision preset (e.g. grass is OverlapOnlyPawn, not blocking)
            smc.set_collision_profile_name(profile)
        if f.get('cull_end'):
            try:
                smc.set_editor_property('ld_max_draw_distance', float(f['cull_end']))
            except Exception:
                pass


def _vec(v):
    return unreal.Vector(*[float(x) for x in v])


def _vec2(v):
    return unreal.Vector2D(float(v[0]), float(v[1]))


def build_spline(sp, idx):
    """One landscape spline mesh (road, kerb, fence segment) as a SplineMeshActor."""
    mesh = load_asset(sp.get('mesh'), 'StaticMesh')
    if mesh is None:
        return
    act = spawn(unreal.SplineMeshActor, sp['location'], sp['rotation'], sp['scale'],
                'Road_%s_%d' % (sp['mesh'].split('.')[-1], idx), 'Roads')
    if act is None:
        return
    c = act.get_component_by_class(unreal.SplineMeshComponent)
    try:
        c.set_mobility(unreal.ComponentMobility.STATIC)
    except Exception:
        pass
    c.set_static_mesh(mesh)
    for mi, m in enumerate(sp.get('materials') or []):
        mat = load_asset(m) if m else None
        if mat:
            c.set_material(mi, mat)
    axis = getattr(unreal.SplineMeshAxis, sp.get('forward_axis', 'X'), unreal.SplineMeshAxis.X)
    c.set_forward_axis(axis, False)
    c.set_spline_up_dir(_vec(sp.get('up_dir', [0, 0, 1])), False)
    c.set_start_and_end(_vec(sp['start_pos']), _vec(sp['start_tangent']),
                        _vec(sp['end_pos']), _vec(sp['end_tangent']), False)
    c.set_start_scale(_vec2(sp.get('start_scale', [1, 1])), False)
    c.set_end_scale(_vec2(sp.get('end_scale', [1, 1])), False)
    c.set_start_roll(float(sp.get('start_roll', 0)), False)
    c.set_end_roll(float(sp.get('end_roll', 0)), False)
    c.set_start_offset(_vec2(sp.get('start_offset', [0, 0])), False)
    c.set_end_offset(_vec2(sp.get('end_offset', [0, 0])), False)
    _set(c, 'b_smooth_interp_roll_scale', bool(sp.get('smooth_roll_scale')))
    c.set_cast_shadow(bool(sp.get('cast_shadow', True)))
    if sp.get('collision_profile'):
        c.set_collision_profile_name(sp['collision_profile'])
    if sp.get('cull_end'):
        _set(c, 'ld_max_draw_distance', float(sp['cull_end']))
    c.update_mesh()


def open_or_create(path):
    if EAL.does_asset_exist(path):
        return EL.load_level(path)
    return EL.new_level(path)


def clear_previous(level_path):
    # only actors that live in this level: opening a persistent level loads its sublevels too, and those were
    # rebuilt moments earlier in the same run
    pkg = level_path + '.'
    for act in EL.get_all_level_actors():
        if act.get_path_name().startswith(pkg) and TAG in [str(t) for t in act.get_editor_property('tags')]:
            EL.destroy_actor(act)


CANCELLED = {'on': False}  # the user pressed Cancel on a progress dialog: stop after this level, don't record it as done
CHECKPOINT_EVERY = 1000  # actors between level saves; a crash then loses at most this many
ACTORS_PER_TICK = 100  # actors placed per editor frame


def editor_memory_mb():
    """The editor's committed memory (what runs out first: 'Ran out of memory' is a commit failure)."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                                               'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                                               'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.PagefileUsage // (1 << 20), c.WorkingSetSize // (1 << 20)
    except Exception:
        pass
    return None


def _free_memory():
    _asset_cache.clear()
    _class_cache.clear()
    try:
        unreal.SystemLibrary.collect_garbage()
    except Exception:
        pass


def requeue_refs(acts, spawned):
    """On resume: the actors placed before the last save are kept, but their links to other actors (an objective's
    capture zone, spawn zones, links into other levels) and their TODO lines were only held in memory by the run
    that stopped. Applying their properties again from the layout queues those links again; the other properties
    are set to the same values they already have."""
    n = 0
    for a in acts:
        act = spawned.get(a['name'])
        if act is None:
            continue
        CUR['actor'] = a['name']
        apply_props(act, a.get('props'))
        comps = components_by_name(act)
        for cname, c in (a.get('components') or {}).items():
            comp = comps.get(cname)
            if comp is not None:
                apply_props(comp, c.get('props'), skip=('StaticMesh', 'OverrideMaterials'))
        n += 1
    log('resumed level: links of %d earlier actor(s) queued again' % n)


def build_level(path, data, prog=None, name=None):
    """prog/name: the run's progress record. The level is saved every CHECKPOINT_EVERY actors and the position is
    recorded, so a run that dies partway through a big level (Metro's persistent level: 11k actors) resumes from
    the last save instead of starting the level over."""
    path = remap(path)
    if not open_or_create(path):
        log('could not open/create ' + path)
        return
    part = (prog or {}).get('partial') or {}
    start = part.get('index', 0) if part.get('level') == name else 0
    spawned = {}
    CUR['level'] = path
    if start:
        # actors placed before the last save are still in the level: keep them, so references can find them
        for act in EL.get_all_level_actors():
            if act.get_path_name().startswith(path + '.') and TAG in [str(t) for t in act.get_editor_property('tags')]:
                spawned[act.get_actor_label()] = act
        log('%s: resuming at actor %d of %d' % (short(path), start, len(data['actors'])))
        requeue_refs(data['actors'][:start], spawned)
    else:
        clear_previous(path)
    _foliage_actors['n'] = 0
    mem = editor_memory_mb()
    if mem:
        log('%s: start, editor memory %d MB committed, %d MB in use' % (short(path), mem[0], mem[1]))
    acts = data['actors']
    i, cancelled = start, False
    while i < len(acts) and not cancelled:
        # one batch per editor frame. The editor only hands back per-frame memory when a frame finishes, and
        # placing a whole level in one go never lets one finish: Metro's 11k-actor level ran a 32 GB machine out
        # of memory at ~7 GB per 1000 actors, with saves and garbage collection freeing none of it.
        end = min(i + ACTORS_PER_TICK, len(acts))
        with unreal.ScopedSlowTask(end - i, 'Rebuilding %s: actor %d of %d' % (short(path), end, len(acts))) as task:
            task.make_dialog(True)
            for j in range(i, end):
                if task.should_cancel():
                    cancelled = True
                    break
                task.enter_progress_frame(1)
                build_actor(acts[j], spawned)
                if prog is not None and j + 1 < len(acts) and (j + 1) % CHECKPOINT_EVERY == 0:
                    EL.save_current_level()
                    prog['partial'] = {'level': name, 'index': j + 1}
                    save_progress(prog)
                    _free_memory()
                    mem = editor_memory_mb()
                    if mem:
                        log('%s: checkpoint at actor %d, editor memory %d MB committed' % (short(path), j + 1, mem[0]))
        i = j if cancelled else end  # on cancel, j is the first actor that was not placed
        yield
    if cancelled:
        EL.save_current_level()
        if prog is not None:
            prog['partial'] = {'level': name, 'index': i}
            save_progress(prog)
        CANCELLED['on'] = True
        log('%s: cancelled at actor %d of %d; Rebuild map again to resume there' % (short(path), i, len(acts)))
        return
    with unreal.ScopedSlowTask(len(data['foliage']) + 1, 'Rebuilding %s: foliage' % short(path)) as task:
        task.make_dialog(True)
        for i, f in enumerate(data['foliage']):
            task.enter_progress_frame(1)
            build_foliage(f, i)
        for i, sp in enumerate(data.get('splines') or []):
            build_spline(sp, i)
    for obj, pname, ref in deferred_refs:
        refs = ref if isinstance(ref, list) else [ref]
        missing = [x for x in refs if x not in spawned]
        if missing:
            log('unresolved ref %s -> %s' % (pname, ', '.join(missing)))
        found = [spawned[x] for x in refs if x in spawned]
        if not found:
            continue
        try:
            assign(obj, pname, found if isinstance(ref, list) else found[0])
        except Exception as ex:
            log('ref %s -> %s: %s' % (pname, ref, ex))
    del deferred_refs[:]
    EL.save_current_level()
    log('%s: done (%d actors, %d foliage types)' % (short(path), len(spawned), len(data['foliage'])))
    # big maps (TORO: 40 levels, 18k actors) run the editor out of memory if each level's objects pile up.
    # The caches hold Python references to every mesh/material/texture used so far, which keeps them all
    # loaded; drop them so garbage collection can actually free the level's assets.
    _free_memory()
    mem = editor_memory_mb()
    if mem:
        log('%s: end, editor memory %d MB committed' % (short(path), mem[0]))


def add_streaming(streaming):
    world = EL.get_editor_world()
    existing = set()
    try:
        for lvl in unreal.EditorLevelUtils.get_levels(world):
            existing.add(lvl.get_outer().get_name())
    except Exception:
        pass
    for s in streaming:
        lp = remap(s['level'])
        if short(lp) in existing:
            continue
        cls = unreal.LevelStreamingAlwaysLoaded if s['type'] == 'LevelStreamingAlwaysLoaded' \
            else unreal.LevelStreamingDynamic
        try:
            unreal.EditorLevelUtils.add_level_to_world(world, lp, cls)
        except Exception as ex:
            log('streaming %s: %s' % (lp, ex))
    EL.save_all_dirty_levels()


SCENARIO_FLAG_LEFT = []  # lighting scenario levels that could not be flagged from Python


def mark_lighting_scenarios(lighting):
    """Flag each lighting scenario sublevel as a Lighting Scenario, as the original had them. Without the flag,
    a lighting build stores lightmaps in every level, so whichever scenario was built last is what all scenarios
    show in game (and building with both visible bakes both suns and every night lamp into one lightmap)."""
    want = {short(v) for v in lighting.values()}
    if not want:
        return
    world = EL.get_editor_world()
    done = set()
    try:
        levels = unreal.EditorLevelUtils.get_levels(world)
    except Exception:
        levels = []
    for lvl in levels:
        name = lvl.get_outer().get_name()
        if name not in want:
            continue
        for prop in ('is_lighting_scenario', 'bIsLightingScenario'):
            try:
                if not lvl.get_editor_property(prop):
                    lvl.set_editor_property(prop, True)
                    lvl.modify()
                done.add(name)
                break
            except Exception:
                continue
    left = sorted(want - done)
    if done:
        log('lighting scenario flag set on %s' % ', '.join(sorted(done)))
        EL.save_all_dirty_levels()
    SCENARIO_FLAG_LEFT.extend(l for l in left if l not in SCENARIO_FLAG_LEFT)


# ------------------------------------------------------------------ textures + material instances
def _file_hash(path):
    import hashlib
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def import_texture(t, dest):
    src = os.path.join(HERE, 'textures', t['file'])
    exists = EAL.does_asset_exist(dest)
    if not os.path.exists(src):
        if exists:
            return unreal.load_asset(dest)
        log('texture file missing next to script: ' + src)
        return None
    digest = _file_hash(src)
    if exists:
        tex = unreal.load_asset(dest)
        # re-import when the extracted image changed (e.g. a newer extractor decoded it correctly)
        if tex is not None and EAL.get_metadata_tag(tex, 'smr_src') == digest:
            return tex
        log('re-importing texture %s (its extracted image changed)' % dest)
    task = unreal.AssetImportTask()
    task.set_editor_property('replace_existing', True)
    task.set_editor_property('filename', src)
    task.set_editor_property('destination_path', dest.rsplit('/', 1)[0])
    task.set_editor_property('destination_name', short(dest))
    task.set_editor_property('automated', True)
    task.set_editor_property('save', True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    tex = unreal.load_asset(dest)
    if tex is None:
        log('texture import failed: ' + dest)
        return None
    apply_props(tex, t.get('props'))
    EAL.set_metadata_tag(tex, 'smr_src', digest)
    EAL.save_loaded_asset(tex)
    log('imported texture ' + dest)
    return tex


_stand_in = {}
STAND_IN_KINDS = {'opaque': ('M_SMR_StandIn', None),
                  'masked': ('M_SMR_StandIn_Masked', 'BLEND_MASKED'),
                  'translucent': ('M_SMR_StandIn_Translucent', 'BLEND_TRANSLUCENT')}


def stand_in_kind(blend):
    b = (blend or '').upper()
    if 'MASKED' in b:
        return 'masked'
    if any(k in b for k in ('TRANSLUCENT', 'ADDITIVE', 'MODULATE', 'ALPHACOMPOSITE', 'ALPHAHOLDOUT')):
        return 'translucent'
    return 'opaque'


STAND_IN_FORMAT = '6'  # bump when the stand-in graph changes; older parents get rebuilt in place
# 6: the channel selector is appended to float4 before the dot with RGBA (5 failed to compile: grey stand-ins)
TCS = unreal.TextureCompressionSettings
# gloss sources go into whichever sampler matches the texture as it is; textures are never modified
GLOSS_SLOTS = (('', 'SAMPLERTYPE_LINEAR_COLOR'), ('C', 'SAMPLERTYPE_COLOR'), ('M', 'SAMPLERTYPE_MASKS'))


def _black_png(path):
    """A 4x4 black PNG, written by hand (no imaging library inside the editor)."""
    import struct as st
    import zlib

    def chunk(tag, data):
        return st.pack('>I', len(data)) + tag + data + st.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''.join(b'\x00' + b'\x00\x00\x00' * 4 for _ in range(4))
    with open(path, 'wb') as fh:
        fh.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', st.pack('>IIBBBBB', 4, 4, 8, 2, 0, 0, 0)) +
                 chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def smr_black(slot):
    """The tool's own black default texture for one sampler type ('' linear, 'C' colour, 'M' masks)."""
    name = {'': 'T_SMR_BlackLinear', 'C': 'T_SMR_BlackColour', 'M': 'T_SMR_BlackMasks'}[slot]
    path = mod_root() + '/SMR/' + name
    if not EAL.does_asset_exist(path):
        fn = os.path.join(HERE, name + '.png')
        _black_png(fn)
        t = unreal.AssetImportTask()
        t.set_editor_property('filename', fn)
        t.set_editor_property('destination_path', path.rsplit('/', 1)[0])
        t.set_editor_property('automated', True)
        t.set_editor_property('save', False)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
    tex = unreal.load_asset(path)
    want = {'': (False, TCS.TC_DEFAULT), 'C': (True, TCS.TC_DEFAULT), 'M': (False, TCS.TC_MASKS)}[slot]
    if (tex.get_editor_property('srgb'), tex.get_editor_property('compression_settings')) != want:
        tex.set_editor_property('srgb', want[0])
        tex.set_editor_property('compression_settings', want[1])
        EAL.save_loaded_asset(tex)
    return tex


def gloss_slot(tex):
    """Which of the stand-in's gloss samplers this texture can go into as it is, or None."""
    try:
        cs = tex.get_editor_property('compression_settings')
        srgb = bool(tex.get_editor_property('srgb'))
    except Exception:
        return None
    if cs == TCS.TC_MASKS:
        return 'M'
    if cs in (TCS.TC_DEFAULT, TCS.TC_BC7, TCS.TC_HDR, TCS.TC_HDR_COMPRESSED, TCS.TC_VECTOR_DISPLACEMENTMAP):
        return 'C' if srgb else ''
    return None  # grayscale/alpha/normal maps need sampler types the stand-in doesn't have


def repair_gloss_textures(layout):
    """v0.7 switched gloss textures to linear/default compression, including game ones (masks), which broke
    every game material using them. Put them back the way they were."""
    orig = {t.get('asset'): (t.get('props') or {}) for t in layout.get('textures') or []}
    seen = set()
    for m in (layout.get('materials') or {}).values():
        for k in ('rough_tex', 'metal_tex'):
            p = (m.get('gloss') or {}).get(k)
            if not p or p in seen:
                continue
            seen.add(p)
            tex = unreal.load_object(None, remap(p) + '.' + short(p))
            if tex is None:
                continue
            if p in orig:
                pr = orig[p]
                want_srgb = pr.get('SRGB', True)
                want_cs = getattr(TCS, py_name(str(pr.get('CompressionSettings') or 'TC_Default').split('::')[-1]).upper(),
                                  TCS.TC_DEFAULT)
            elif p.startswith('/Game/') and short(p).upper().endswith('_M'):
                want_srgb, want_cs = False, TCS.TC_MASKS  # stock game mask textures
            else:
                continue
            if (bool(tex.get_editor_property('srgb')), tex.get_editor_property('compression_settings')) != \
                    (bool(want_srgb), want_cs):
                tex.set_editor_property('srgb', bool(want_srgb))
                tex.set_editor_property('compression_settings', want_cs)
                EAL.save_loaded_asset(tex)
                log('restored texture settings on ' + p)


def _stand_in_graph(mat, kind):
    """BaseColor x Tint, Normal, and roughness/metallic = saturate(value + amount * texture.R), where the
    texture sits in whichever of three samplers matches its settings (the others stay black)."""
    MEL = unreal.MaterialEditingLibrary
    MP = unreal.MaterialProperty
    X = unreal

    def node(cls, x, y, param=None, default=None):
        e = MEL.create_material_expression(mat, cls, x, y)
        if param:
            e.set_editor_property('parameter_name', param)
        if default is not None:
            e.set_editor_property('default_value', default)
        return e
    bc = node(X.MaterialExpressionTextureSampleParameter2D, -900, -300, 'BaseColor')
    bc.set_editor_property('texture', unreal.load_asset('/Engine/EngineResources/DefaultTexture'))
    tint = node(X.MaterialExpressionVectorParameter, -900, -500, 'Tint', unreal.LinearColor(1, 1, 1, 1))
    mul = node(X.MaterialExpressionMultiply, -450, -350)
    MEL.connect_material_expressions(bc, 'RGB', mul, 'A')
    MEL.connect_material_expressions(tint, '', mul, 'B')
    MEL.connect_material_property(mul, '', MP.MP_BASE_COLOR)
    if kind == 'masked':
        MEL.connect_material_property(bc, 'A', MP.MP_OPACITY_MASK)
    if kind == 'translucent':
        op = node(X.MaterialExpressionScalarParameter, -900, -150, 'Opacity', 1.0)
        omul = node(X.MaterialExpressionMultiply, -450, -150)
        MEL.connect_material_expressions(bc, 'A', omul, 'A')
        MEL.connect_material_expressions(op, '', omul, 'B')
        MEL.connect_material_property(omul, '', MP.MP_OPACITY)
    nm = node(X.MaterialExpressionTextureSampleParameter2D, -900, 0, 'Normal')
    nm.set_editor_property('sampler_type', unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    nm.set_editor_property('texture', unreal.load_asset('/Engine/EngineMaterials/DefaultNormal'))
    MEL.connect_material_property(nm, 'RGB', MP.MP_NORMAL)
    for i, (pre, val, prop) in enumerate((('Rough', 0.5, MP.MP_ROUGHNESS), ('Metal', 0.0, MP.MP_METALLIC))):
        y = 300 + 700 * i
        base = node(X.MaterialExpressionScalarParameter, -900, y, 'Roughness' if pre == 'Rough' else 'Metallic', val)
        # which channel of the texture carries the value: a one-hot vector parameter dotted with RGBA (default R)
        chan = node(X.MaterialExpressionVectorParameter, -900, y + 40, pre + 'Channel', unreal.LinearColor(1, 0, 0, 0))
        # a vector parameter's main output is RGB only (float3); the dot with the texture's RGBA (float4) needs all
        # four, or the parent fails to compile and every stand-in renders grey
        chan4 = node(X.MaterialExpressionAppendVector, -820, y + 40)
        MEL.connect_material_expressions(chan, '', chan4, 'A')
        MEL.connect_material_expressions(chan, 'A', chan4, 'B')
        total = None
        for j, (suf, st) in enumerate(GLOSS_SLOTS):
            tex = node(X.MaterialExpressionTextureSampleParameter2D, -900, y + 80 + 180 * j, pre + 'Tex' + suf)
            tex.set_editor_property('sampler_type', getattr(unreal.MaterialSamplerType, st))
            tex.set_editor_property('texture', smr_black(suf))
            dot = node(X.MaterialExpressionDotProduct, -750, y + 80 + 180 * j)
            MEL.connect_material_expressions(tex, 'RGBA', dot, 'A')
            MEL.connect_material_expressions(chan4, '', dot, 'B')
            if total is None:
                total, out = dot, ''
            else:
                add = node(X.MaterialExpressionAdd, -650, y + 80 + 180 * j)
                MEL.connect_material_expressions(total, out, add, 'A')
                MEL.connect_material_expressions(dot, '', add, 'B')
                total, out = add, ''
        amt = node(X.MaterialExpressionScalarParameter, -900, y + 640, pre + 'TexAmount', 0.0)
        m2 = node(X.MaterialExpressionMultiply, -500, y + 300)
        MEL.connect_material_expressions(total, out, m2, 'A')
        MEL.connect_material_expressions(amt, '', m2, 'B')
        add = node(X.MaterialExpressionAdd, -350, y + 200)
        MEL.connect_material_expressions(base, '', add, 'A')
        MEL.connect_material_expressions(m2, '', add, 'B')
        sat = node(X.MaterialExpressionSaturate, -200, y + 200)
        MEL.connect_material_expressions(add, '', sat, '')
        MEL.connect_material_property(sat, '', prop)


def stand_in_parent(kind='opaque'):
    """A plain material that stand-ins for custom materials use (see _stand_in_graph).
    Blend mode lives on the parent itself: overriding it to translucent on a material instance crashes the
    4.27 renderer (uniform buffer mismatch), so each blend mode gets its own parent."""
    if kind in _stand_in:
        return _stand_in[kind]
    name, blend = STAND_IN_KINDS[kind]
    path = mod_root() + '/SMR/' + name
    MEL = unreal.MaterialEditingLibrary
    if EAL.does_asset_exist(path):
        mat = unreal.load_asset(path)
        if EAL.get_metadata_tag(mat, 'smr_standin_format') == STAND_IN_FORMAT:
            _stand_in[kind] = mat
            return mat
        MEL.delete_all_material_expressions(mat)  # older graph: rebuild it in place, instances keep pointing here
        log('upgrading stand-in parent material ' + path)
    else:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            name, path.rsplit('/', 1)[0], unreal.Material, unreal.MaterialFactoryNew())
        log('created stand-in parent material ' + path)
    if blend:
        _set(mat, 'blend_mode', getattr(unreal.BlendMode, blend))
    _stand_in_graph(mat, kind)
    for flag in ('used_with_instanced_static_meshes', 'used_with_static_lighting'):
        try:
            mat.set_editor_property(flag, True)
        except Exception:
            pass
    MEL.recompile_material(mat)
    EAL.set_metadata_tag(mat, 'smr_standin_format', STAND_IN_FORMAT)
    EAL.save_loaded_asset(mat)
    _stand_in[kind] = mat
    return mat


def apply_gloss(mi, g):
    """Roughness/metallic read from the original's compiled shaders (see smr/gloss.py)."""
    if not g:
        return
    MEL = unreal.MaterialEditingLibrary
    vals = {'Roughness': g.get('rough', 0.5), 'RoughTexAmount': 0.0, 'Metallic': 0.0, 'MetalTexAmount': 0.0}
    for pre, key in (('Rough', 'rough'), ('Metal', 'metal')):
        slot = None
        if g.get(key + '_tex'):
            t = load_asset(g[key + '_tex'] + '.' + short(g[key + '_tex']), 'Texture2D')
            slot = gloss_slot(t) if t else None
            if slot is None:
                log('gloss %s: %s texture %s unusable, left out' % (mi.get_name(), pre.lower(), g[key + '_tex']))
                if pre == 'Rough':
                    vals['Roughness'] = 0.5  # it needed that texture: middle-of-the-road instead of a mirror
            else:
                vals[pre + 'TexAmount'] = g.get(key + '_amt', 1.0)
        for suf, _ in GLOSS_SLOTS:
            MEL.set_material_instance_texture_parameter_value(
                mi, pre + 'Tex' + suf, t if suf == slot else smr_black(suf))
    for name, val in vals.items():
        MEL.set_material_instance_scalar_parameter_value(mi, name, float(val))
    chans = {}
    for pre, key in (('Rough', 'rough'), ('Metal', 'metal')):
        ch = (g.get(key + '_ch') or 'R').upper()[:1]
        chans[pre] = ch
        MEL.set_material_instance_vector_parameter_value(mi, pre + 'Channel', unreal.LinearColor(
            *[1.0 if c == ch else 0.0 for c in 'RGBA']))
    log('gloss %s: roughness %.2f%s' % (mi.get_name(), vals['Roughness'],
                                         ' + %.2f x texture.%s' % (vals['RoughTexAmount'], chans['Rough'])
                                         if vals['RoughTexAmount'] else ''))


def _usable(tex, role):
    """Only plug a texture into a sampler whose type matches, or the material fails to compile."""
    try:
        cs = tex.get_editor_property('compression_settings')
        is_normal = cs == unreal.TextureCompressionSettings.TC_NORMALMAP
        if role == 'Normal':
            return is_normal
        return not is_normal and bool(tex.get_editor_property('srgb'))
    except Exception:
        return role != 'Normal'


def set_overrides(mi, ov):
    """Two-sided / opacity clip overrides on a material instance. Blend-mode overrides are dropped on purpose:
    switching an instance between opaque and translucent crashes the 4.27 renderer."""
    ov = {k: v for k, v in (ov or {}).items() if 'BlendMode' not in k}
    if not ov:
        return
    try:
        bpo = mi.get_editor_property('base_property_overrides')
        apply_props(bpo, ov)
        mi.set_editor_property('base_property_overrides', bpo)
    except Exception as ex:
        log('overrides on %s: %s' % (mi.get_name(), ex))


def finish_instance(mi, m):
    if m.get('PhysMaterial') or m.get('phys_material'):
        pm = load_asset(m.get('PhysMaterial') or m.get('phys_material'))
        if pm:
            try:
                mi.set_editor_property('phys_material', pm)
            except Exception:
                pass
    set_overrides(mi, m.get('BasePropertyOverrides'))
    try:
        unreal.MaterialEditingLibrary.update_material_instance(mi)
    except Exception:
        pass
    EAL.save_loaded_asset(mi)


def stand_in_textures(mi, m, dest):
    MEL = unreal.MaterialEditingLibrary
    used = []
    for role, key in (('BaseColor', 'base_color'), ('Normal', 'normal')):
        if not m.get(key):
            continue
        tex = load_asset(m[key] + '.' + short(m[key]), 'Texture2D')
        if tex is None:
            log('stand-in %s: %s texture %s not found' % (short(dest), role, m[key]))
        elif not isinstance(tex, unreal.Texture):
            log('stand-in %s: %s %s is a %s, not a texture; skipped' % (short(dest), role, m[key], tex.get_class().get_name()))
        elif not _usable(tex, role):
            log('stand-in %s: %s texture %s skipped (wrong sRGB/compression for that slot)' % (short(dest), role, m[key]))
        else:
            MEL.set_material_instance_texture_parameter_value(mi, role, tex)
            used.append(role)
    return used


def _default_opacity(mi, m, used):
    """A translucent stand-in without a colour texture (water, glass) has nothing to take its alpha from; a flat
    opaque plane is the worst reading of it, so it starts half-transparent."""
    if stand_in_kind(m.get('blend_mode')) == 'translucent' and 'BaseColor' not in used:
        try:
            unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(mi, 'Opacity', 0.45)
        except Exception:
            pass


def create_stand_in(m, dest):
    MEL = unreal.MaterialEditingLibrary
    mi = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        short(dest), dest.rsplit('/', 1)[0], unreal.MaterialInstanceConstant,
        unreal.MaterialInstanceConstantFactoryNew())
    MEL.set_material_instance_parent(mi, stand_in_parent(stand_in_kind(m.get('blend_mode'))))
    used = stand_in_textures(mi, m, dest)
    _default_opacity(mi, m, used)
    apply_gloss(mi, m.get('gloss'))
    if not m.get('base_color'):
        dark = 'black' in short(dest).lower()
        MEL.set_material_instance_vector_parameter_value(mi, 'Tint', unreal.LinearColor(
            *((0.02, 0.02, 0.02, 1) if dark else (0.5, 0.5, 0.5, 1))))
    ov = {}
    if m.get('two_sided'):
        ov.update({'bOverride_TwoSided': True, 'TwoSided': True})
    if m.get('opacity_mask_clip') is not None and stand_in_kind(m.get('blend_mode')) == 'masked':
        ov.update({'bOverride_OpacityMaskClipValue': True, 'OpacityMaskClipValue': m['opacity_mask_clip']})
    m = dict(m, BasePropertyOverrides=ov or None)
    finish_instance(mi, m)
    log('created stand-in for custom material %s (%s)' % (dest, ', '.join(used) or 'flat colour'))
    return mi


def _under_game_water(mat):
    """True when this material (or a parent of it) is the game water material a water stand-in was swapped to."""
    if mat is None:
        return False
    water = game_water_material()  # cached; also right on a resumed run where the swap happened last time
    depth = 0
    while water is not None and mat is not None and depth < 12:
        if mat is water:
            return True
        mat = mat.get_editor_property('parent') if isinstance(mat, unreal.MaterialInstance) else None
        depth += 1
    return False


def _adopt_game_water(mi, dest, parent=None):
    """A child of a swapped water stand-in inherits the game water shader directly. It is touched as little as
    possible: re-parent and save, nothing else. Clearing or writing parameters, or update_material_instance, on an
    instance under the game water shader while the level renders it trips a render-thread assertion in 4.27
    (UniformBuffer layout mismatch) and takes the editor down."""
    water = game_water_material()
    if parent is not None:
        unreal.MaterialEditingLibrary.set_material_instance_parent(mi, water)
        EAL.save_loaded_asset(mi)
        log('water: %s now uses the game material %s (child of a swapped water shader)' % (short(dest), water.get_name()))
    elif mi.get_editor_property('parent') is not water:
        unreal.MaterialEditingLibrary.set_material_instance_parent(mi, water)
        EAL.save_loaded_asset(mi)
        log('water: %s now uses the game material %s' % (short(dest), water.get_name()))
    return mi


def create_material(m, dest):
    if EAL.does_asset_exist(dest):
        obj = unreal.load_asset(dest)
        if isinstance(obj, unreal.MaterialInstanceConstant) and _under_game_water(obj.get_editor_property('parent')):
            return _adopt_game_water(obj, dest)
        if isinstance(obj, unreal.MaterialInstanceConstant):
            # start from a clean slate: a value an earlier run set (a tint, a texture in the wrong slot) would
            # otherwise survive, since re-applying only adds or changes overrides and never removes one
            try:
                unreal.MaterialEditingLibrary.clear_all_material_instance_parameters(obj)
            except Exception as ex:
                log('could not clear parameters on %s: %s' % (short(dest), ex))
        if m.get('substitute') and isinstance(obj, unreal.MaterialInstanceConstant):
            stand_in_parent(stand_in_kind(m.get('blend_mode')))  # upgrades an older parent graph
            used = stand_in_textures(obj, m, dest)  # re-apply, and log why if a texture can't be used
            _default_opacity(obj, m, used)
            apply_gloss(obj, m.get('gloss'))
            finish_instance(obj, m)
        elif isinstance(obj, unreal.MaterialInstanceConstant):
            set_instance_params(obj, m)  # refreshed in place from the current extract
            finish_instance(obj, m)
        if isinstance(obj, unreal.MaterialInstanceConstant):
            log('refreshed material instance %s%s' % (dest, ' (tint: %s)' % ', '.join(
                k for k, v in (m.get('vector_roles') or {}).items() if v) if m.get('vector_roles') else ''))
        return obj
    if m.get('substitute'):
        return create_stand_in(m, dest)
    parent = load_asset(m.get('Parent'))
    if parent is None:
        todo('material instance %s: parent %s missing' % (dest, m.get('Parent')))
        return None
    MEL = unreal.MaterialEditingLibrary
    mi = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        short(dest), dest.rsplit('/', 1)[0], unreal.MaterialInstanceConstant,
        unreal.MaterialInstanceConstantFactoryNew())
    if _under_game_water(parent):
        return _adopt_game_water(mi, dest, parent)
    MEL.set_material_instance_parent(mi, parent)
    set_instance_params(mi, m)
    finish_instance(mi, m)
    log('created material instance ' + dest)
    return mi


def set_instance_params(mi, m):
    MEL = unreal.MaterialEditingLibrary
    for p in m.get('ScalarParameterValues') or []:
        MEL.set_material_instance_scalar_parameter_value(mi, p['ParameterInfo']['Name'], p['ParameterValue'])
    vroles = m.get('vector_roles')  # parent is a stand-in: route its colour to Tint
    for p in m.get('VectorParameterValues') or []:
        name = p['ParameterInfo']['Name']
        val = list(p['ParameterValue'][:4])
        if vroles is not None:
            name = vroles.get(name)
            if not name:
                continue
            val = val[:3] + [1.0]
        MEL.set_material_instance_vector_parameter_value(mi, name, unreal.LinearColor(*val))
    roles = m.get('param_roles')  # parent is a stand-in: route textures to its BaseColor/Normal slots
    for p in m.get('TextureParameterValues') or []:
        tex = load_asset(p.get('ParameterValue'))
        name = p['ParameterInfo']['Name']
        if not isinstance(tex, unreal.Texture):
            continue  # a material function or collection the original fed into a texture parameter
        if roles is not None:
            name = roles.get(name)
            if not name or not _usable(tex, name):
                continue
        if not MEL.set_material_instance_texture_parameter_value(mi, name, tex):
            # the parent has no such parameter (renamed in a game update?): it shows the parent's default there
            log('material instance %s: parameter %s not found on its parent; %s left out' % (
                mi.get_name(), name, tex.get_name()))
    if roles is not None or vroles is not None:
        apply_gloss(mi, m.get('gloss'))


def build_fallback(pkg):
    """An asset the old mod shipped under /Game that today's game doesn't have: rebuild it inside the mod."""
    dest = remap(FALLBACK[pkg])
    obj = None
    m = (LAYOUT_DATA.get('materials') or {}).get(pkg)
    mesh = (LAYOUT_DATA.get('meshes') or {}).get(pkg)
    if m and 'error' not in m:
        obj = create_material(m, dest)
    elif mesh:
        obj = import_one_mesh(mesh, dest)
    else:
        t = next((t for t in LAYOUT_DATA.get('textures', []) if t.get('asset') == pkg and 'file' in t), None)
        if t:
            obj = import_texture(t, dest)
    if obj is not None:
        log('rebuilt missing %s inside the mod as %s' % (pkg, dest))
        return unreal.load_object(None, dest + '.' + short(dest)) or obj
    return None


def import_textures(layout):
    for t in layout.get('textures', []):
        if 'file' in t and not t.get('lazy'):
            import_texture(t, remap(t['asset']))


def create_materials(layout):
    mats = layout.get('materials') or {}
    done = set()

    def ensure(pkg, depth=0):
        if pkg in done or depth > 20:
            return
        done.add(pkg)
        m = mats[pkg]
        if 'error' in m:
            return
        dest = remap(pkg)
        if m.get('lazy'):
            # a /Game asset the game may still have: built only on demand (build_fallback). But one an earlier run
            # already built inside the mod is refreshed here, so re-running picks up extractor fixes for it
            dest = remap(FALLBACK[pkg]) if pkg in FALLBACK else None
            if not dest or not EAL.does_asset_exist(dest):
                return
        par = (m.get('Parent') or '').split('.')[0]
        if par in mats:  # parents first, so children don't cache a missing parent
            ensure(par, depth + 1)
        obj = create_material(m, dest)
        if obj is not None:
            _asset_cache.pop(dest + '.' + short(dest), None)
            _asset_cache.pop(dest, None)

    for pkg in mats:
        ensure(pkg)
    swap_water_stand_ins(mats)


_water_material = {}


def game_water_material():
    """A finished water material from the game, if the SDK has one (river preferred)."""
    if 'm' not in _water_material:
        reg = unreal.AssetRegistryHelpers.get_asset_registry()
        cands = [a for a in reg.get_assets_by_path('/Game/Environment', True)
                 if str(a.asset_class) in ('Material', 'MaterialInstanceConstant')
                 and 'water' in str(a.asset_name).lower() and 'decal' not in str(a.asset_name).lower()]
        cands.sort(key=lambda a: ('river' not in str(a.asset_name).lower(), len(str(a.asset_name))))
        _water_material['m'] = cands[0].get_asset() if cands else None
    return _water_material['m']


def swap_water_stand_ins(mats):
    """A custom water shader can't be rebuilt, and a flat stand-in reads as paint. When the game has a water
    material, water stand-ins become children of it instead, so every mesh, foliage and spline using them changes."""
    water = None
    for pkg, m in mats.items():
        if not m.get('substitute') or 'error' in m or stand_in_kind(m.get('blend_mode')) != 'translucent':
            continue
        names = [short(pkg)] + [short(k) for k, c in mats.items() if obj_path_of(c.get('Parent') or '') == pkg]
        if not any('water' in n.lower() or 'river' in n.lower() or 'lake' in n.lower() for n in names):
            continue
        dest = remap(FALLBACK[pkg]) if pkg in FALLBACK else remap(pkg)
        if not EAL.does_asset_exist(dest):
            continue
        if water is None:
            water = game_water_material()
            if water is None:
                todo('WATER: %s is a water shader the tool cannot rebuild; assign one of the game\'s water materials '
                     'to the water meshes by hand.' % short(pkg))
                return
        mi = unreal.load_asset(dest)
        if isinstance(mi, unreal.MaterialInstanceConstant) and mi.get_editor_property('parent') is not water:
            unreal.MaterialEditingLibrary.set_material_instance_parent(mi, water)  # only this: see _adopt_game_water
            EAL.save_loaded_asset(mi)
            log('water: %s now uses the game material %s (the original water shader cannot be rebuilt)' % (
                short(pkg), water.get_name()))


# ------------------------------------------------------------------ custom static meshes
def _set(obj, name, value):
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception:
        return False


def fbx_options(m):
    ui = unreal.FbxImportUI()
    for k, v in (('import_mesh', True), ('import_as_skeletal', False), ('import_materials', False),
                 ('import_textures', False), ('import_animations', False), ('create_physics_asset', False)):
        _set(ui, k, v)
    _set(ui, 'mesh_type_to_import', unreal.FBXImportType.FBXIT_STATIC_MESH)
    sm = ui.get_editor_property('static_mesh_import_data')
    for k, v in (('combine_meshes', True), ('generate_lightmap_u_vs', False), ('auto_generate_collision', False),
                 ('remove_degenerates', False), ('build_adjacency_buffer', False), ('one_convex_hull_per_ucx', True),
                 ('convert_scene', True), ('force_front_x_axis', False), ('convert_scene_unit', False),
                 ('transform_vertex_to_absolute', True), ('import_uniform_scale', 1.0)):
        _set(sm, k, v)
    # Extracts before r39 decoded the cooked normals as unsigned bytes (they have been signed since UE 4.20), so
    # their FBX normals are wrong: a mesh lit with them renders near-black while its albedo looks right in Unlit.
    # Those get normals computed from the geometry. From r39 the FBX carries the original normals (custom foliage
    # and bevel normals included) and they are imported as they are; tangents are still computed.
    _set(sm, 'normal_import_method', unreal.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS if m.get('normals_ok')
         else unreal.FBXNormalImportMethod.FBXNIM_COMPUTE_NORMALS)
    _set(sm, 'vertex_color_import_option', unreal.VertexColorImportOption.REPLACE if m.get('colors')
         else unreal.VertexColorImportOption.IGNORE)
    _set(ui, 'static_mesh_import_data', sm)
    return ui


def check_bounds(mesh, m):
    """Guard against an axis/handedness mix-up on import: compare with the original bounds."""
    try:
        bmin, bmax = mesh.get_bounding_box().min, mesh.get_bounding_box().max
    except Exception:
        try:
            b = mesh.get_bounds()
            o, e = b.origin, b.box_extent
            bmin, bmax = o - e, o + e
        except Exception:
            return None
    (x0, y0, z0), (x1, y1, z1) = m['bounds']
    tol = 1.0 + 0.01 * max(x1 - x0, y1 - y0, z1 - z0)
    try:
        got = tuple(round(float(v), 2) for v in (bmin.x, bmin.y, bmin.z, bmax.x, bmax.y, bmax.z))
    except Exception:
        return None
    want = (x0, y0, z0, x1, y1, z1)
    return all(abs(g - w) <= tol for g, w in zip(got, want)), got, want


def set_normal_build(mesh, m):
    """The mesh builder's normal settings on every LOD, to match the import (see fbx_options): the original
    normals kept for r39+ extracts, recomputed from the geometry for older ones whose FBX normals are wrong.
    Tangents are always computed (the FBX has none). The build settings are set explicitly because the importer
    and the mesh builder are different paths, and a mesh re-imported over an older one keeps the old settings."""
    recompute = not m.get('normals_ok')
    try:
        esl = unreal.EditorStaticMeshLibrary
        for i in range(esl.get_lod_count(mesh)):
            bs = esl.get_lod_build_settings(mesh, i)
            if bool(bs.get_editor_property('recompute_normals')) == recompute and \
                    bs.get_editor_property('recompute_tangents'):
                continue
            bs.set_editor_property('recompute_normals', recompute)
            bs.set_editor_property('recompute_tangents', True)
            esl.set_lod_build_settings(mesh, i, bs)
    except Exception as ex:
        log('normals on %s: %s' % (mesh.get_name(), ex))


def finish_mesh(mesh, m):
    set_normal_build(mesh, m)
    _set(mesh, 'light_map_coordinate_index', int(m.get('lightmap_index') or 1))
    if m.get('lightmap_res'):
        _set(mesh, 'light_map_resolution', int(m['lightmap_res']))
    bs = None
    try:
        bs = mesh.get_editor_property('body_setup')
    except Exception:
        pass
    if bs is not None:
        if m.get('trace'):
            s = m['trace'].split('::')[-1]
            flag = getattr(unreal.CollisionTraceFlag, py_name(s).upper(), None)
            if flag is not None:
                _set(bs, 'collision_trace_flag', flag)
                if 'COMPLEX_AS_SIMPLE' in str(flag).upper():
                    # the collision cooked at import doesn't pick up the new flag until the editor restarts;
                    # simple shapes go unused with complex-as-simple, so clearing them rebuilds it right now
                    try:
                        unreal.EditorStaticMeshLibrary.remove_collisions(mesh)
                    except Exception:
                        pass
        else:
            # the original kept the default. Set it explicitly: a mesh re-imported over one that r31 and earlier
            # wrongly switched to complex-as-simple keeps that flag otherwise, and its hulls would go unused
            _set(bs, 'collision_trace_flag', unreal.CollisionTraceFlag.CTF_USE_DEFAULT)
        if m.get('profile') and m['profile'] not in ('Custom',):
            try:
                bi = bs.get_editor_property('default_instance')
                bi.set_editor_property('collision_profile_name', m['profile'])
                bs.set_editor_property('default_instance', bi)
            except Exception:
                pass
        # a mesh that walks on simple collision but lost its hull in the FBX import has nothing to stand on;
        # its triangles are the next best thing
        # get_simple_collision_count only counts boxes, spheres and capsules; convex hulls (every UCX_ the FBX
        # carries) are get_convex_collision_count. Counting only the first called every hull "missing" and threw
        # away the original collision of every mesh (MallConflict: 300 of 300 meshes that had hulls)
        try:
            E = unreal.EditorStaticMeshLibrary
            flag_now = str(bs.get_editor_property('collision_trace_flag')).upper()
            if ('COMPLEX_AS_SIMPLE' not in flag_now and
                    E.get_simple_collision_count(mesh) + E.get_convex_collision_count(mesh) == 0):
                _set(bs, 'collision_trace_flag', unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
                unreal.EditorStaticMeshLibrary.remove_collisions(mesh)
                log('%s: hull missing after import, using its triangles for collision' % mesh.get_name())
        except Exception as ex:
            log('hull check on %s: %s' % (mesh.get_name(), ex))
    # the FBX importer can leave sections with collision switched off; the original had it on
    try:
        E = unreal.EditorStaticMeshLibrary
        n = E.get_num_sections(mesh, 0) if hasattr(E, 'get_num_sections') else E.get_number_materials(mesh)
        for si in range(n):
            E.enable_section_collision(mesh, True, 0, si)
    except Exception as ex:
        log('section collision on %s: %s' % (mesh.get_name(), ex))
    assign_slots(mesh, m)
    add_lods(mesh, m)
    EAL.save_loaded_asset(mesh)


LOD_MIN_TRIS = 400  # below this a mesh is cheap enough that LODs aren't worth it


def imported_mesh(pkg, m):
    """The mesh asset for one entry of layout['meshes'], if the project actually has it.

    'lazy' only means the rebuild does not import this mesh up front -- it is brought in when an actor needs
    it. It does NOT mean the mesh is absent, so the single-job passes must not skip it: on TORO that quietly
    left the 159 heaviest meshes (every SM_MERGED_*, up to 22k triangles) without LODs and without their
    material slots, which are exactly the ones that need both."""
    path = remap(pkg)
    try:
        if not EAL.does_asset_exist(path.split('.')[0]):
            return None
    except Exception:
        pass
    return unreal.load_asset(path)


def add_lods(mesh, m):
    """The extractor only carries LOD0, so imported meshes draw every triangle at any distance (the original
    shipped LODs). Generate 3 reduced LODs for anything with real geometry."""
    if (m.get('tris') or 0) < LOD_MIN_TRIS:
        return False
    E = unreal.EditorStaticMeshLibrary
    try:
        if E.get_lod_count(mesh) > 1:
            return False
        opts = unreal.EditorScriptingMeshReductionOptions()
        rows = []
        for pct, size in ((1.0, 1.0), (0.5, 0.5), (0.25, 0.25), (0.1, 0.1)):
            r = unreal.EditorScriptingMeshReductionSettings()
            r.set_editor_property('percent_triangles', pct)
            r.set_editor_property('screen_size', size)
            rows.append(r)
        opts.set_editor_property('reduction_settings', rows)
        opts.set_editor_property('auto_compute_lod_screen_size', True)
        return E.set_lods(mesh, opts) > 1
    except Exception as ex:
        log('LODs for %s: %s' % (mesh.get_name(), ex))
        return False


def assign_slots(mesh, m):
    """Point each material slot at the original material. Returns True if anything changed.
    Elements of an unreal.Array of structs are copies: editing them in a loop changes nothing, so the slots
    are rebuilt as a new list and written back, and UStaticMesh.set_material is used where available."""
    changed = False
    try:
        slots = list(mesh.get_editor_property('static_materials'))
        want_all = m.get('materials') or []
        for i, s in enumerate(slots):
            if i >= len(want_all):
                break
            want = want_all[i]
            if str(s.get_editor_property('material_slot_name')) != (want.get('slot') or 'Material'):
                s.set_editor_property('material_slot_name', want.get('slot') or 'Material')
                changed = True
            path = want.get('material')
            if path and not path.startswith('/Engine/'):
                mat = load_asset(path)
                if mat is None:
                    log('mesh %s slot %d: material %s not found' % (mesh.get_name(), i, path))
                elif s.get_editor_property('material_interface') != mat:
                    s.set_editor_property('material_interface', mat)
                    changed = True
            slots[i] = s
        if changed:
            mesh.set_editor_property('static_materials', slots)
            for i, s in enumerate(slots):
                try:
                    mesh.set_material(i, s.get_editor_property('material_interface'))
                except Exception:
                    break  # not exposed in this SDK; the property write above has to do
    except Exception as ex:
        log('material slots on %s: %s' % (mesh.get_name(), ex))
    return changed


def _layer_textures(layer, textures):
    """Colour and normal map for a landscape paint layer, picked from the original material's textures by name:
    T_Beach_D / T_Beach_N for layer Beach; GrassDry falls back to Grass's normal map."""
    import re
    def pick(stem, suffixes):
        best = None
        for t in textures:
            n = short(t).lower()
            for suf in suffixes:
                if n == 't_%s%s' % (stem, suf):
                    return t
                if best is None and re.search(r'(^|_)%s(_|$)' % stem, n) and n.endswith(suf) and 'detail' not in n:
                    best = t
        return best
    low = layer.lower()
    stems = [low] + ([low[:-3]] if low.endswith('dry') else [])
    colour = next((x for x in (pick(st, ('_d', '_bc', '_albedo', '_diffuse', '_col', '_color', '_basecolor', '_c'))
                               for st in stems) if x), None)
    normal = next((x for x in (pick(st, ('_n', '_normal', '_nrm', '_nor')) for st in stems) if x), None)
    return colour, normal


def _slope_projection(mat, tiling, scale):
    """UVs that project textures sideways (world X or Y, whichever the face points along) and a 0..1 mask that
    is 1 on steep slopes. Top-down UVs stretch a texture into vertical streaks on cliffs; landscape packs blend
    to a side projection there, and this does the same at the same texture size."""
    MEL = unreal.MaterialEditingLibrary
    X = -2600
    def node(cls, y, **props):
        n = MEL.create_material_expression(mat, cls, X, y)
        for k, v in props.items():
            n.set_editor_property(k, v)
        return n
    def link(a, b, pin='', out=''):
        MEL.connect_material_expressions(a, out, b, pin)
    wp = node(unreal.MaterialExpressionWorldPosition, 300)
    nrm = node(unreal.MaterialExpressionVertexNormalWS, 600)
    xz = node(unreal.MaterialExpressionComponentMask, 260, r=True, g=False, b=True, a=False)
    yz = node(unreal.MaterialExpressionComponentMask, 340, r=False, g=True, b=True, a=False)
    link(wp, xz); link(wp, yz)
    ab = node(unreal.MaterialExpressionAbs, 560)
    link(nrm, ab)
    nx = node(unreal.MaterialExpressionComponentMask, 520, r=True, g=False, b=False, a=False)
    ny = node(unreal.MaterialExpressionComponentMask, 600, r=False, g=True, b=False, a=False)
    link(ab, nx); link(ab, ny)
    # facing X -> project onto YZ, facing Y -> XZ, with a short blend between so there's no hard seam
    d = node(unreal.MaterialExpressionSubtract, 560); link(nx, d, 'A'); link(ny, d, 'B')
    d8 = node(unreal.MaterialExpressionMultiply, 560, const_b=8.0); link(d, d8, 'A')
    dh = node(unreal.MaterialExpressionAdd, 560, const_b=0.5); link(d8, dh, 'A')
    w = node(unreal.MaterialExpressionSaturate, 560); link(dh, w)
    pick = node(unreal.MaterialExpressionLinearInterpolate, 300)
    link(xz, pick, 'A'); link(yz, pick, 'B'); link(w, pick, 'Alpha')
    # world cm -> landscape quads (what LandscapeLayerCoords counts in), times the same Tiling
    toq = node(unreal.MaterialExpressionMultiply, 300, const_b=-1.0 / float(scale)); link(pick, toq, 'A')
    side_uv = node(unreal.MaterialExpressionMultiply, 300); link(toq, side_uv, 'A'); link(tiling, side_uv, 'B')
    # slope mask: 0 on ground flatter than ~40 degrees, 1 on anything steeper than ~60
    nz = node(unreal.MaterialExpressionComponentMask, 700, r=False, g=False, b=True, a=False)
    link(nrm, nz)
    sub = node(unreal.MaterialExpressionSubtract, 700, const_a=0.75); link(nz, sub, 'B')
    mul = node(unreal.MaterialExpressionMultiply, 700, const_b=4.0); link(sub, mul, 'A')
    slope = node(unreal.MaterialExpressionSaturate, 700); link(mul, slope)
    return side_uv, slope


def _master_of(orig):
    """The material at the top of a material instance's parent chain (through the mod's own instances), or None."""
    mats = LAYOUT_DATA.get('materials') or {}
    seen, cur = set(), orig
    while cur in mats and cur not in seen:
        m = mats[cur]
        if not isinstance(m, dict) or m.get('substitute') or 'error' in m or m.get('lazy'):
            return None  # a stand-in, an unreadable instance or a /Game copy along the way: not usable as is
        seen.add(cur)
        cur = obj_path_of(m.get('Parent'))
    return cur if cur and cur != orig else None


def _instance_on_game_master(orig):
    """True when the landscape's material is the mod's own instance of a stock game material the SDK has."""
    top = _master_of(orig)
    return bool(top) and top.startswith('/Game/') and top not in (LAYOUT_DATA.get('materials') or {}) \
        and EAL.does_asset_exist(top)


def build_landscape_materials(layout):
    """Each landscape's original material is a custom graph (often a marketplace landscape pack) and can't be
    rebuilt, but its paint layers can: a material with one weight-blended layer per original layer name, each
    using that layer's own textures, so the extracted layer PNGs import straight onto it."""
    MEL = unreal.MaterialEditingLibrary
    MP = unreal.MaterialProperty
    for k in [k for k, v in _asset_cache.items() if v is None]:
        del _asset_cache[k]  # anything missing earlier (e.g. meshes imported since) gets looked up again
    _registry_index.clear()
    mats = LAYOUT_DATA.get('materials') or {}
    # one material per level (it is named after the level): when a level has several landscapes (TORO: a main
    # terrain and a small patch), it needs every layer any of them paints, or the last one built wins
    by_level = {}
    built = built_levels(layout)
    for ls in layout.get('landscapes', []):
        if ls['level'] not in built:
            continue
        for layer in ls.get('layers') or {}:
            lst = by_level.setdefault(ls['level'], [])
            if layer not in lst:
                lst.append(layer)
    done_levels = set()
    for ls in layout.get('landscapes', []):
        if ls['level'] in done_levels or ls['level'] not in built:
            continue
        done_levels.add(ls['level'])
        layers = by_level.get(ls['level']) or []
        if not layers:
            continue
        orig = obj_path_of(ls.get('material'))
        if orig and orig.startswith('/Game/') and orig not in mats and load_asset(ls.get('material')) is not None:
            # a stock game landscape material (TORO uses MI_Terrain_Town_River): the original is right there,
            # with the real layer names and textures; a stand-in could only guess from the names
            log('landscape in %s uses the game material %s; no stand-in needed' % (short(ls['level']), short(orig)))
            todo('LANDSCAPE: in the landscape import panel use the game material %s; its layers match the layer '
                 'files in the landscape folder.' % short(orig))
            continue
        if _instance_on_game_master(orig) and load_asset(ls.get('material')) is not None:
            # the mod's own instance of a stock game master (Panji: MM_Terrain_Sandstorm_GAPInstPanj1 on
            # MM_Terrain_Sandstorm_Citadel): rebuilt above with its textures, and its layers are the master's
            log('landscape in %s uses %s, an instance of the game material %s; rebuilt as is, no stand-in needed' % (
                short(ls['level']), short(orig), short(_master_of(orig))))
            todo('LANDSCAPE: in the landscape import panel use the material %s (the original, rebuilt); its layers '
                 'match the layer files in the landscape folder.' % short(orig))
            continue
        m = mats.get(orig) or {}
        seen = set()
        textures = []
        while m and obj_path_of(m.get('Parent')) not in seen:
            # the instances along the chain carry the per-layer textures (marketplace packs keep the master generic)
            for tp in m.get('TextureParameterValues') or []:
                tv = obj_path_of(tp.get('ParameterValue')) if isinstance(tp.get('ParameterValue'), str) else None
                if tv and tv not in textures:
                    textures.append(tv)
            if m.get('substitute'):
                break
            seen.add(obj_path_of(m.get('Parent')))
            m = mats.get(obj_path_of(m.get('Parent'))) or {}
        textures += [t for t in (m.get('textures') or []) if t not in textures]
        textures = [t for t in textures if not t.startswith('/Engine/')]
        name = 'M_SMR_Landscape_' + short(ls['level'])
        path = mod_root() + '/SMR/' + name
        # never edit the graph of a material a landscape is using: the editor recompiles it and every component's
        # instance while the graph is half built, and 4.27 crashes on a shader thread doing that. Build a fresh
        # material next to it, point the landscape at the new one, then retire the old (same as swapping the
        # material by hand in the Details panel)
        # every earlier build of this material still around (the canonical name and any _buildN a previous run
        # could not retire); landscapes using any of them move to the new one
        folder = path.rsplit('/', 1)[0]
        reg = unreal.AssetRegistryHelpers.get_asset_registry()
        # Materials only: an instance made from a _buildN material is auto-named <it>_Inst and must not be retired
        previous = [str(a.object_path).split('.')[0] for a in reg.get_assets_by_path(folder, False)
                    if str(a.asset_class) == 'Material' and (str(a.asset_name) == name or
                                                             re.match(re.escape(name) + r'_build\d+$',
                                                                      str(a.asset_name)))]
        old_mats = [unreal.load_asset(p) for p in previous]
        old_mats = [o for o in old_mats if o is not None]
        build_path = path
        n = 0
        while EAL.does_asset_exist(build_path):
            n += 1
            build_path = '%s_build%d' % (path, n)
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            short(build_path), folder, unreal.Material, unreal.MaterialFactoryNew())
        coords = MEL.create_material_expression(mat, unreal.MaterialExpressionLandscapeLayerCoords, -1600, 0)
        tiling = MEL.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -1600, 120)
        tiling.set_editor_property('parameter_name', 'Tiling')
        tiling.set_editor_property('default_value', 0.25)  # one repeat every 4 m; adjust on the instance
        uv = MEL.create_material_expression(mat, unreal.MaterialExpressionMultiply, -1400, 40)
        MEL.connect_material_expressions(coords, '', uv, 'A')
        MEL.connect_material_expressions(tiling, '', uv, 'B')
        side_uv, slope = _slope_projection(mat, tiling, (ls.get('scale') or [100.0])[0] or 100.0)
        blends = {}
        for kind, prop, y in (('colour', MP.MP_BASE_COLOR, -400), ('normal', MP.MP_NORMAL, 400)):
            lb = MEL.create_material_expression(mat, unreal.MaterialExpressionLandscapeLayerBlend, -300, y)
            ins = []
            for i, layer in enumerate(layers):
                li = unreal.LayerBlendInput()
                li.set_editor_property('layer_name', layer)
                li.set_editor_property('blend_type', unreal.LandscapeLayerBlendType.LB_WEIGHT_BLEND)
                li.set_editor_property('preview_weight', 1.0 if i == 0 else 0.0)
                ins.append(li)
            lb.set_editor_property('layers', ins)
            MEL.connect_material_property(lb, '', prop)
            blends[kind] = lb
        used = []
        found = {l: _layer_textures(l, textures) for l in layers}
        base = next((v for v in found.values() if v[0]), (None, None))
        for i, layer in enumerate(layers):
            colour, normal = found[layer]
            if not colour:
                # utility layers (e.g. RemoveProcedural) have no textures of their own; show the base ground there
                colour, normal = base
            for kind, tex_path in (('colour', colour), ('normal', normal)):
                y = -1400 + 260 * i + (130 if kind == 'normal' else 0)
                tex = load_asset(tex_path + '.' + short(tex_path), 'Texture2D') if tex_path else None
                if tex is not None and not _usable(tex, 'Normal' if kind == 'normal' else 'BaseColor'):
                    tex = None
                if tex is None:
                    if kind == 'normal':
                        tex = unreal.load_asset('/Engine/EngineMaterials/DefaultNormal')
                    else:
                        c = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -900, y)
                        c.set_editor_property('constant', unreal.LinearColor(0.35, 0.3, 0.25, 1))
                        MEL.connect_material_expressions(c, '', blends[kind], 'Layer ' + layer)
                        continue
                smp = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -900, y)
                smp.set_editor_property('texture', tex)
                # 10 layers x 2 textures is past the 16-sampler limit; shared samplers lift it (otherwise the
                # components that use many layers fail to compile and show the grey checker)
                smp.set_editor_property('sampler_source', unreal.SamplerSourceMode.SSM_WRAP_WORLD_GROUP_SETTINGS)
                if kind == 'normal':
                    smp.set_editor_property('sampler_type', unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
                MEL.connect_material_expressions(uv, '', smp, 'UVs')
                # steep faces: the same texture projected from the side, so cliffs don't smear into streaks
                side = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample, -900, y + 60)
                side.set_editor_property('texture', tex)
                side.set_editor_property('sampler_source', unreal.SamplerSourceMode.SSM_WRAP_WORLD_GROUP_SETTINGS)
                if kind == 'normal':
                    side.set_editor_property('sampler_type', unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
                MEL.connect_material_expressions(side_uv, '', side, 'UVs')
                mix = MEL.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -600, y)
                MEL.connect_material_expressions(smp, 'RGB', mix, 'A')
                MEL.connect_material_expressions(side, 'RGB', mix, 'B')
                MEL.connect_material_expressions(slope, '', mix, 'Alpha')
                if not MEL.connect_material_expressions(mix, '', blends[kind], 'Layer ' + layer):
                    log('landscape material: could not wire %s %s' % (layer, kind))
            used.append('%s=%s' % (layer, short(colour) if colour else 'flat'))
        try:
            grass = landscape_grass_for(layers)
        except Exception as ex:
            grass = []
            log('landscape grass skipped: %s' % ex)
        if grass:
            go = MEL.create_material_expression(mat, unreal.MaterialExpressionLandscapeGrassOutput, 0, 1300)
            gins = []
            for layer, lg in grass:
                gi = unreal.GrassInput()
                gi.set_editor_property('name', lg.get_name())  # several grass types can share one layer
                gi.set_editor_property('grass_type', lg)
                gins.append(gi)
            go.set_editor_property('grass_types', gins)
            for j, (layer, lg) in enumerate(grass):
                smp = MEL.create_material_expression(mat, unreal.MaterialExpressionLandscapeLayerSample,
                                                     -300, 1300 + 120 * j)
                smp.set_editor_property('parameter_name', layer)
                if not MEL.connect_material_expressions(smp, '', go, lg.get_name()):
                    log('landscape grass: could not wire %s (%s)' % (lg.get_name(), layer))
            used.append('grass: ' + ', '.join('%s on %s' % (lg.get_name(), l) for l, lg in grass))
        rough = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -300, 900)
        rough.set_editor_property('r', 0.85)
        MEL.connect_material_property(rough, '', MP.MP_ROUGHNESS)
        MEL.recompile_material(mat)
        EAL.save_loaded_asset(mat)
        if old_mats:
            moved = 0
            for la in _landscape_actors():
                try:
                    if la.get_editor_property('landscape_material') in old_mats:
                        la.set_editor_property('landscape_material', mat)
                        moved += 1
                except Exception as ex:
                    log('landscape %s: could not swap material: %s' % (la.get_actor_label(), ex))
            if moved:
                EL.save_all_dirty_levels()
                log('landscape material: %d landscape(s) moved to the rebuilt material' % moved)
            old_paths = [o.get_path_name().split('.')[0] for o in old_mats]
            old_mats = None
            _asset_cache.clear()
            try:
                unreal.SystemLibrary.collect_garbage()
            except Exception:
                pass
            left = []
            for op in old_paths:
                if not EAL.delete_asset(op):
                    left.append(short(op))
            if not left and EAL.rename_asset(build_path, path):
                mat = unreal.load_asset(path)
            else:
                if left:
                    log('landscape material: %s still referenced somewhere, left in place; the rebuilt one is %s' % (
                        ', '.join(left), short(build_path)))
                path = build_path
        log('landscape material %s: %s' % (path, ', '.join(used)))
        todo('LANDSCAPE: in the landscape import panel use material %s (not %s); its layers match the layer '
             'files in the landscape folder.' % (short(path), short(orig)))


VISIBILITY_LAYER = '__LandscapeVisibility__'   # ALandscapeProxy::VisibilityLayer's LayerName in 4.27
HOLES_LEFT = []    # landscapes whose visibility mask could not be applied from Python
LAYERINFO_LEFT = []  # (landscape, layer) whose LayerInfo could not be assigned from Python


def _landscape_actors():
    """Every Landscape actor in the levels that are loaded now, by actor label and by level."""
    out = []
    try:
        for a in unreal.GameplayStatics.get_all_actors_of_class(EL.get_editor_world(), unreal.Landscape):
            out.append(a)
    except Exception:
        for a in EL.get_all_level_actors():
            if isinstance(a, unreal.Landscape):
                out.append(a)
    return out


def _match_landscape(ls, actors):
    """The imported Landscape actor for one extracted landscape: by level first, then by position, since the
    import panel names the actor whatever it likes but puts it where the original was."""
    want = ls.get('actor_location') or []
    best, best_d = None, None
    for a in actors:
        lvl = None
        try:
            lvl = a.get_outer().get_outer().get_name()
        except Exception:
            pass
        if lvl and short(ls['level']) and lvl != short(ls['level']):
            continue
        if not want:
            return a
        loc = a.get_actor_location()
        d = max(abs(loc.x - want[0]), abs(loc.y - want[1]))
        if best_d is None or d < best_d:
            best, best_d = a, d
    # the import panel rounds, and a landscape can be nudged afterwards; a metre of slack is plenty
    return best if best is not None and (best_d is None or best_d <= 100.0) else None


def _layer_info_asset(layer, folder):
    """The ULandscapeLayerInfoObject for one paint layer, created next to the landscape material if the project
    has not got one yet. A layer with no LayerInfo holds no weight data at all, so a layered terrain material
    resolves to near-black no matter what the layer PNGs contain."""
    name = '%s_LayerInfo' % layer
    path = '%s/%s' % (folder, name)
    existing = unreal.load_asset(path) if EAL.does_asset_exist(path) else None
    if existing is not None:
        return existing
    # the original's own LayerInfo assets come across with the mod, under whatever folder it kept them in
    alt = _index('LandscapeLayerInfoObject').get(name)
    if alt:
        found = unreal.load_object(None, alt)
        if found is not None:
            return found
    try:
        f = unreal.LandscapeLayerInfoObjectFactory() if hasattr(unreal, 'LandscapeLayerInfoObjectFactory') else None
        obj = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            name, folder, unreal.LandscapeLayerInfoObject, f)
        if obj is None:
            return None
        obj.set_editor_property('layer_name', layer)
        EAL.save_loaded_asset(obj)
        return obj
    except Exception as ex:
        log('layer info %s: %s' % (path, ex))
        return None


def _import_png(src, dest):
    """A loose PNG next to the script imported as a Texture2D, with no sRGB or compression so its bytes survive
    (a weight mask read through sRGB is not the mask that was painted)."""
    if not os.path.exists(src):
        return None
    task = unreal.AssetImportTask()
    task.set_editor_property('replace_existing', True)
    task.set_editor_property('filename', src)
    task.set_editor_property('destination_path', dest.rsplit('/', 1)[0])
    task.set_editor_property('destination_name', short(dest))
    task.set_editor_property('automated', True)
    task.set_editor_property('save', False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    tex = unreal.load_asset(dest)
    if tex is None:
        return None
    for prop, val in (('srgb', False),
                      ('compression_settings', unreal.TextureCompressionSettings.TC_VECTOR_DISPLACEMENTMAP),
                      ('mip_gen_settings', unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS)):
        try:
            tex.set_editor_property(prop, val)
        except Exception:
            pass
    return tex


def _png_render_target(png, w, h):
    """The layer PNG as a render target of the landscape's own resolution, which is the only shape
    LandscapeImportWeightmapFromRenderTarget accepts."""
    dest = '%s/Landscape/T_SMR_Mask_%s' % (mod_root() or '/Game', short(png))
    tex = _import_png(png, dest) if png else None
    if tex is None:
        return None
    rt = unreal.RenderingLibrary.create_render_target2d(w, h, unreal.TextureRenderTargetFormat.RTF_RGBA8)
    if rt is None:
        return None
    world = EL.get_editor_world()
    unreal.RenderingLibrary.clear_render_target2d(world, rt, unreal.LinearColor(0, 0, 0, 1))
    canvas, size, ctx = unreal.RenderingLibrary.begin_draw_canvas_to_render_target(world, rt)
    try:
        canvas.draw_texture(tex, unreal.Vector2D(0, 0), unreal.Vector2D(w, h), unreal.Vector2D(0, 0),
                            unreal.Vector2D(1, 1), unreal.LinearColor(1, 1, 1, 1),
                            unreal.BlendMode.BLEND_OPAQUE)
    finally:
        unreal.RenderingLibrary.end_draw_canvas_to_render_target(world, ctx)
    return rt


def apply_landscape_settings(layout):
    """Everything about a landscape that the import panel cannot carry: the visibility mask that cuts the holes,
    the LayerInfo each paint layer needs, and the phys material its footsteps and impacts come from.

    The visibility mask matters most. The original's holes (stairwells, trenches, cellars, culverts) live in the
    engine's DataLayer weightmap, which the import panel has no field for, so without this pass the terrain is
    solid and fills every one of them in: cover entrances are sealed and anything underground is unreachable."""
    lss = layout.get('landscapes') or []
    if not lss:
        return
    actors = _landscape_actors()
    if not actors:
        log('no Landscape actor is in the levels that are loaded; import the landscape first, then run '
            '"Finish landscape" again')
        HOLES_LEFT.extend(short(ls['level']) for ls in lss if ls.get('visibility_layer'))
        return
    for ls in lss:
        lvl = short(ls['level'])
        land = _match_landscape(ls, actors)
        if land is None:
            log('landscape for %s is not imported yet; skipped' % lvl)
            if ls.get('visibility_layer'):
                HOLES_LEFT.append(lvl)
            continue
        # phys material: footstep sounds, bullet impacts and decals all come off this, and a landscape
        # imported by hand has it empty
        phys = obj_path_of(ls.get('phys_material'))
        if phys:
            pm = load_asset(phys, 'PhysicalMaterial')
            if pm is not None:
                try:
                    land.set_editor_property('default_phys_material', pm)
                    log('%s: phys material %s' % (lvl, short(phys)))
                except Exception as ex:
                    log('%s: phys material %s' % (lvl, ex))
            else:
                todo('LANDSCAPE: set the landscape in %s to Default Phys Material %s (it is not in the project).'
                     % (lvl, short(phys)))
        for prop, key in (('collision_mip_level', 'collision_mip_level'),
                          ('static_lighting_lod', 'static_lighting_lod'),
                          ('static_lighting_resolution', 'lightmap_res')):
            v = ls.get(key)
            if v is None:
                continue
            try:
                land.set_editor_property(prop, v)
            except Exception:
                pass
        folder = '%s/Landscape' % (mod_root() or '/Game')
        try:
            info = land.get_editor_property('landscape_info')
        except Exception:
            info = None
        # LayerInfo per paint layer. Painting a layer whose LayerInfo is empty writes nothing, which is what
        # left the terrain black on the first TORO rebuild.
        for layer in (ls.get('layers') or {}):
            obj = _layer_info_asset(layer, folder)
            if obj is None:
                LAYERINFO_LEFT.append((lvl, layer))
                continue
            ok = False
            try:
                for fn in ('replace_layer_info', 'set_layer_info'):
                    if info is not None and hasattr(info, fn):
                        getattr(info, fn)(layer, obj)
                        ok = True
                        break
            except Exception as ex:
                log('%s layer %s: %s' % (lvl, layer, ex))
            if not ok:
                LAYERINFO_LEFT.append((lvl, layer))
        # the visibility mask, last: it changes the collision as well as the look, so Build Paths comes after
        vis = ls.get('visibility_layer')
        if not vis:
            continue
        png = os.path.join(HERE, 'landscape', vis)
        if not os.path.exists(png):
            log('%s: %s is not in the landscape folder' % (lvl, vis))
            HOLES_LEFT.append(lvl)
            continue
        w, h = ls.get('resolution') or [0, 0]
        try:
            rt = _png_render_target(png, int(w), int(h))
            if rt is None:
                raise RuntimeError('the layer PNG would not import')
            land.landscape_import_weightmap_from_render_target(rt, VISIBILITY_LAYER)
            log('%s: visibility mask applied, %dx%d' % (lvl, w, h))
            EL.save_all_dirty_levels()
            todo('LANDSCAPE: the landscape in %s had holes cut in it, so run Build > Build Paths before you cook '
                 '(navigation still thinks the terrain is solid there).' % lvl)
        except Exception as ex:
            log('%s: visibility mask could not be applied (%s)' % (lvl, ex))
            HOLES_LEFT.append(lvl)


def landscape_grass_for(layers):
    """Rebuild the original's landscape grass types (the auto-spawned ground grass) and pair each with the paint
    layer of the same name (LG_Grass -> Grass). Returns [(layer, grass type asset)]."""
    out = []
    by_low = {l.lower(): l for l in layers}
    for pkg, props in (LAYOUT_DATA.get('grass_types') or {}).items():
        nm = short(pkg)
        for pre in ('LG_', 'GT_', 'LGT_'):
            if nm.upper().startswith(pre):
                nm = nm[len(pre):]
        # LGT_Grass -> Grass; LGT_RW_Plants_Fern -> Plants (the first word that is a paint layer)
        words = [nm.lower()] + [w for w in nm.lower().split('_') if w]
        layer = next((by_low[w] for w in words if w in by_low), None)
        if not layer:
            log('landscape grass type %s not used: this landscape has no %s layer' % (short(pkg), nm))
            continue
        path = remap(FALLBACK[pkg]) if pkg in FALLBACK else remap(pkg)
        if EAL.does_asset_exist(path):
            lg = unreal.load_asset(path)
        else:
            # LandscapeGrassType isn't a DataAsset in 4.27; it has its own factory (None lets AssetTools pick it)
            f = unreal.LandscapeGrassTypeFactory() if hasattr(unreal, 'LandscapeGrassTypeFactory') else None
            lg = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                short(pkg), path.rsplit('/', 1)[0], unreal.LandscapeGrassType, f)
        if lg is None:
            log('landscape grass type %s could not be created' % path)
            continue
        vs = []
        for d in props.get('GrassVarieties') or []:
            gv = unreal.GrassVariety()
            apply_props(gv, d)
            vs.append(gv)
        lg.set_editor_property('grass_varieties', vs)
        EAL.save_loaded_asset(lg)
        log('landscape grass type %s: %d variet%s on layer %s' % (path, len(vs), 'y' if len(vs) == 1 else 'ies', layer))
        out.append((layer, lg))
    return out


def obj_path_of(p):
    return p.split('.')[0] if p else p


def _missing_events(layout):
    out = set()
    for d in layout['levels'].values():
        for a in d['actors']:
            for c in (a.get('components') or {}).values():
                ev = (c.get('props') or {}).get('AkAudioEvent')
                if isinstance(ev, str) and not ev.startswith('/Game/'):
                    out.add(ev.split('.')[0])
    return out


MESH_FORMAT = '4'  # bump when the FBX export or mesh import changes; older imports get re-imported in place
# 3: r32 keeps the imported hulls (r31 and earlier dropped them all and switched to complex-as-simple)
# 4: r39 imports the original normals from extracts that decode them correctly (normals_ok). Meshes from older
#    extracts stay at 3: nothing about their import changed, so they aren't re-imported for nothing


def mesh_format(m):
    return MESH_FORMAT if m.get('normals_ok') else '3'


def _needs_import(dest, m):
    if not EAL.does_asset_exist(dest):
        return True
    try:
        return EAL.get_metadata_tag(unreal.load_asset(dest), 'smr_mesh_format') != mesh_format(m)
    except Exception:
        return False


def import_one_mesh(m, dest):
    src = os.path.join(HERE, *m['file'].split('/'))
    if not os.path.exists(src):
        log('mesh file missing next to script: ' + src)
        return None
    if _needs_import(dest, m):
        t = unreal.AssetImportTask()
        for k, v in (('filename', src), ('destination_path', dest.rsplit('/', 1)[0]),
                     ('destination_name', short(dest)), ('automated', True), ('replace_existing', True),
                     ('save', False), ('options', fbx_options(m))):
            t.set_editor_property(k, v)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
    mesh = unreal.load_asset(dest)
    if mesh is not None:
        finish_mesh(mesh, m)
        EAL.set_metadata_tag(mesh, 'smr_mesh_format', mesh_format(m))
        EAL.save_loaded_asset(mesh)
    return mesh


def import_meshes(layout):
    items = []
    fixed = 0
    for pkg, m in sorted((layout.get('meshes') or {}).items()):
        if m.get('lazy'):
            continue  # a /Game mesh: imported only if the SDK lacks it (build_fallback)
        if _needs_import(remap(pkg), m):
            items.append((pkg, m))
            continue
        # imported on an earlier run: its materials may not have existed yet, so check the slots every time
        mesh = unreal.load_asset(remap(pkg))
        if mesh is not None and assign_slots(mesh, m):
            EAL.save_loaded_asset(mesh)
            fixed += 1
    if fixed:
        log('re-linked materials on %d meshes imported earlier' % fixed)
    if not items:
        return True
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    done = 0
    checked = False
    with unreal.ScopedSlowTask(len(items), 'Importing %d meshes' % len(items)) as task:
        task.make_dialog(True)
        for start in range(0, len(items), 20):
            if task.should_cancel():
                break
            batch = []
            for pkg, m in items[start:start + 20]:
                src = os.path.join(HERE, *m['file'].split('/'))
                if not os.path.exists(src):
                    log('mesh file missing next to script: ' + src)
                    continue
                dest = remap(pkg)
                t = unreal.AssetImportTask()
                t.set_editor_property('filename', src)
                t.set_editor_property('destination_path', dest.rsplit('/', 1)[0])
                t.set_editor_property('destination_name', short(dest))
                t.set_editor_property('automated', True)
                t.set_editor_property('replace_existing', True)
                t.set_editor_property('save', False)
                t.set_editor_property('options', fbx_options(m))
                batch.append((t, pkg, m, dest))
            tools.import_asset_tasks([b[0] for b in batch])
            for t, pkg, m, dest in batch:
                mesh = unreal.load_asset(dest)
                if mesh is None:
                    log('mesh import failed: ' + dest)
                    continue
                if not checked and m.get('bounds'):
                    res = check_bounds(mesh, m)
                    if res is not None:
                        checked = True
                        if not res[0]:
                            log('STOPPED: %s imported with bounds %s, expected %s. The FBX axis conversion is off in '
                                'this SDK; send rebuild_log.txt to the tool author.' % (dest, res[1], res[2]))
                            return False
                        log('mesh axis check passed on ' + dest)
                finish_mesh(mesh, m)
                try:
                    EAL.set_metadata_tag(mesh, 'smr_mesh_format', mesh_format(m))
                    saved = EAL.save_loaded_asset(mesh, False)  # False: save even if not marked dirty, so False means failed
                except Exception as ex:
                    saved, why = False, ex
                else:
                    why = 'the editor refused to save it'
                if not saved:
                    # it would vanish when the editor closes, with nothing saying why
                    todo('mesh %s was imported but could not be saved (%s): save it by hand (Save All), or run Rebuild '
                         'map again' % (dest, why))
                done += 1
            task.enter_progress_frame(len(items[start:start + 20]))
    log('imported %d of %d custom meshes' % (done, len(items)))
    return True


# ------------------------------------------------------------------ blueprints
JUMP_PADS = {}       # original class path -> launch velocity / component settings
SHARED_PAD = {}      # 'class' -> the one Blueprint every jump pad uses
PIN_TAIL = ('PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,'
            'bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,bAdvancedView=False,bOrphanedPin=False,')


def _pin(pid, name, out, cat, sub='None', default=None, links=(), array=False):
    s = 'CustomProperties Pin (PinId=%s,PinName="%s",' % (pid, name)
    if out:
        s += 'Direction="EGPD_Output",'
    s += ('PinType.PinCategory="%s",PinType.PinSubCategory="",PinType.PinSubCategoryObject=%s,'
          'PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=%s,'
          'PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,'
          'PinType.bIsUObjectWrapper=False,' % (cat, sub, 'Array' if array else 'None'))
    if default is not None:
        s += 'DefaultValue="%s",' % default
    if links:
        s += 'LinkedTo=(%s),' % ''.join('%s %s,' % l for l in links)
    return '   ' + s + PIN_TAIL + ')'


def jump_pad_graph():
    """Overlap -> Cast To Character -> Launch Character, velocity read from the pad's first actor tag
    ("X=.. Y=.. Z=.."), so one Blueprint serves every pad with its own original velocity."""
    ids = iter('%032X' % (0x5A000000 + i) for i in range(1, 99))
    P = {k: next(ids) for k in ('e_out', 'e_then', 'e_other', 'c_exec', 'c_then', 'c_fail', 'c_obj', 'c_as', 'c_ok',
                                'v_self', 'v_tags', 'g_arr', 'g_idx', 'g_out', 'n_in', 'n_ret', 's_in', 's_vec',
                                's_ok', 'l_exec', 'l_then', 'l_self', 'l_vel', 'l_xy', 'l_z')}
    A, CH, OBJ = "Class'/Script/Engine.Actor'", "Class'/Script/Engine.Character'", "Class'/Script/CoreUObject.Object'"
    VEC = "ScriptStruct'/Script/CoreUObject.Vector'"
    STR = "Class'/Script/Engine.KismetStringLibrary'"
    E, C, V, G, N, S, L = ('K2Node_Event_0', 'K2Node_DynamicCast_0', 'K2Node_VariableGet_0', 'K2Node_GetArrayItem_0',
                           'K2Node_CallFunction_1', 'K2Node_CallFunction_2', 'K2Node_CallFunction_0')
    node = lambda cls, name, x, y, extra: ['Begin Object Class=/Script/BlueprintGraph.%s Name="%s"' % (cls, name)] + \
        ['   ' + e for e in extra] + ['   NodePosX=%d' % x, '   NodePosY=%d' % y]
    L_ = []
    L_ += node('K2Node_Event', E, 0, 0, ['EventReference=(MemberParent=%s,MemberName="ReceiveActorBeginOverlap")' % A,
                                        'bOverrideFunction=True']) + [
        _pin(P['e_out'], 'OutputDelegate', True, 'delegate'),
        _pin(P['e_then'], 'then', True, 'exec', links=[(C, P['c_exec'])]),
        _pin(P['e_other'], 'OtherActor', True, 'object', A, links=[(C, P['c_obj'])]), 'End Object']
    L_ += node('K2Node_DynamicCast', C, 300, 0, ['TargetType=%s' % CH]) + [
        _pin(P['c_exec'], 'execute', False, 'exec', links=[(E, P['e_then'])]),
        _pin(P['c_then'], 'then', True, 'exec', links=[(L, P['l_exec'])]),
        _pin(P['c_fail'], 'CastFailed', True, 'exec'),
        _pin(P['c_obj'], 'Object', False, 'object', OBJ, links=[(E, P['e_other'])]),
        _pin(P['c_as'], 'AsCharacter', True, 'object', CH, links=[(L, P['l_self'])]),
        _pin(P['c_ok'], 'bSuccess', True, 'bool'), 'End Object']
    L_ += node('K2Node_VariableGet', V, -200, 250, ['VariableReference=(MemberName="Tags",bSelfContext=True)']) + [
        _pin(P['v_tags'], 'Tags', True, 'name', array=True, links=[(G, P['g_arr'])]), 'End Object']
    L_ += node('K2Node_GetArrayItem', G, 0, 250, []) + [
        _pin(P['g_arr'], 'Array', False, 'name', array=True, links=[(V, P['v_tags'])]),
        _pin(P['g_idx'], 'Dimension 1', False, 'int', default='0'),
        _pin(P['g_out'], 'Output', True, 'name', links=[(N, P['n_in'])]), 'End Object']
    L_ += node('K2Node_CallFunction', N, 200, 250,
               ['bIsPureFunc=True', 'FunctionReference=(MemberParent=%s,MemberName="Conv_NameToString")' % STR]) + [
        _pin(P['n_in'], 'InName', False, 'name', links=[(G, P['g_out'])]),
        _pin(P['n_ret'], 'ReturnValue', True, 'string', links=[(S, P['s_in'])]), 'End Object']
    L_ += node('K2Node_CallFunction', S, 450, 250,
               ['bIsPureFunc=True', 'FunctionReference=(MemberParent=%s,MemberName="Conv_StringToVector")' % STR]) + [
        _pin(P['s_in'], 'InString', False, 'string', links=[(N, P['n_ret'])]),
        _pin(P['s_vec'], 'OutConvertedVector', True, 'struct', VEC, links=[(L, P['l_vel'])]),
        _pin(P['s_ok'], 'OutIsValid', True, 'bool'), 'End Object']
    L_ += node('K2Node_CallFunction', L, 700, 0, ['FunctionReference=(MemberParent=%s,MemberName="LaunchCharacter")' % CH]) + [
        _pin(P['l_exec'], 'execute', False, 'exec', links=[(C, P['c_then'])]),
        _pin(P['l_then'], 'then', True, 'exec'),
        _pin(P['l_self'], 'self', False, 'object', CH, links=[(C, P['c_as'])]),
        _pin(P['l_vel'], 'LaunchVelocity', False, 'struct', VEC, links=[(S, P['s_vec'])]),
        _pin(P['l_xy'], 'bXYOverride', False, 'bool', default='true'),
        _pin(P['l_z'], 'bZOverride', False, 'bool', default='true'), 'End Object']
    return '\r\n'.join(L_) + '\r\n'


def set_clipboard(text):
    try:
        import ctypes
        from ctypes import wintypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        data = text.encode('utf-16-le') + b'\x00\x00'
        h = k.GlobalAlloc(0x0002, len(data))
        ptr = k.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        k.GlobalUnlock(h)
        if not u.OpenClipboard(None):
            return False
        u.EmptyClipboard()
        u.SetClipboardData(13, h)
        u.CloseClipboard()
        return True
    except Exception:
        return False


def bp_class(path):
    """'/Mod/X/BP' -> its generated class, or None."""
    return unreal.load_object(None, '%s.%s_C' % (path, short(path)))


def new_blueprint(path, parent):
    f = unreal.BlueprintFactory()
    f.set_editor_property('parent_class', parent)
    return unreal.AssetToolsHelpers.get_asset_tools().create_asset(short(path), path.rsplit('/', 1)[0],
                                                                   unreal.Blueprint, f)


def build_blueprints(layout):
    bps = layout.get('blueprints') or {}
    # property-only Blueprints: classes it points at (e.g. a game mode's pawn) first
    plain = {k: v for k, v in bps.items() if v.get('kind') == 'property_only'}
    order, seen = [], set()

    def visit(k):
        if k in seen:
            return
        seen.add(k)
        for v in plain[k]['cdo'].values():
            q = v.split('.')[0] if isinstance(v, str) else None
            if q in plain:
                visit(q)
        order.append(k)
    for k in plain:
        visit(k)
    for pkg in order:
        info = plain[pkg]
        dest = remap(pkg)
        if not EAL.does_asset_exist(dest):
            parent = load_class(info['parent']) if info.get('parent') else None
            if parent is None:
                todo('Blueprint %s: parent class %s not found; recreate it by hand' % (dest, info.get('parent')))
                continue
            bp = new_blueprint(dest, parent)
            if bp is None:
                todo('Blueprint %s could not be created' % dest)
                continue
            cls = bp_class(dest)
            if cls is not None and info['cdo']:
                apply_props(unreal.get_default_object(cls), info['cdo'])
            EAL.save_loaded_asset(bp)
            _class_cache.pop(dest + '.' + short(dest) + '_C', None)
            log('rebuilt Blueprint %s (child of %s, %d changed defaults)' % (dest, info['parent'].split('.')[-1],
                                                                            len(info['cdo'])))
    # jump pads: every original pad becomes the one shared pad Blueprint; its velocity goes in an actor tag
    pads = {k: v for k, v in bps.items() if v.get('kind') == 'jump_pad'}
    if not pads:
        return
    for k, v in pads.items():
        JUMP_PADS[k + '.' + short(k) + '_C'] = v
    first = sorted(pads)[0]
    shared = remap(first.rsplit('/', 1)[0] + '/BP_SMR_JumpPad')
    graph = jump_pad_graph()
    with open(os.path.join(HERE, 'BP_SMR_JumpPad_graph.txt'), 'w') as fh:
        fh.write(graph)
    if not EAL.does_asset_exist(shared):
        bp = new_blueprint(shared, unreal.TriggerCapsule)
        EAL.save_loaded_asset(bp)
        log('created %s for the %d jump pads' % (shared, len(pads)))
    cls = bp_class(shared)
    SHARED_PAD['class'] = cls
    compiled = unreal.find_object(None, '%s.%s_C:ExecuteUbergraph_%s' % (shared, short(shared), short(shared)))
    if compiled is None:
        on_clip = set_clipboard(graph)
        todo('JUMP PADS (one paste): open %s, click its Event Graph tab, click in the graph, press Ctrl+V, then '
             'Compile and Save. The graph is %s, and also saved as BP_SMR_JumpPad_graph.txt next to this script. '
             'All %d pads are already placed with their original launch velocities.' % (
                 shared, 'on your clipboard' if on_clip else 'not on the clipboard', len(pads)))


def build_jump_pad(a, info):
    cls = SHARED_PAD.get('class')
    if cls is None:
        return None
    comps = info.get('components') or {}
    cap = (comps.get('Capsule') or {}).get('props') or {}
    s = (cap.get('RelativeScale3D') or [1, 1, 1])[0]
    z = (cap.get('RelativeLocation') or [0, 0, 0])[2] * a['scale'][2]
    loc = [a['location'][0], a['location'][1], a['location'][2] + z]
    act = spawn(cls, loc, a['rotation'], a['scale'], a['name'], 'Gameplay/JumpPads')
    if act is None:
        return None
    v = info['launch_velocity']
    act.set_editor_property('tags', ['X=%s Y=%s Z=%s' % tuple(v), TAG])
    try:
        c = act.get_component_by_class(unreal.CapsuleComponent)
        c.set_capsule_size(float(cap.get('CapsuleRadius', 22.0)) * s, float(cap.get('CapsuleHalfHeight', 44.0)) * s)
    except Exception as ex:
        log('jump pad %s capsule: %s' % (a['name'], ex))
    mc = (comps.get('JumpPad') or {}).get('props') or {}
    if mc.get('StaticMesh'):
        m = spawn(unreal.StaticMeshActor, a['location'], a['rotation'], a['scale'], a['name'] + '_Mesh',
                  'Gameplay/JumpPads')
        if m:
            smc = m.static_mesh_component
            mesh = load_asset(mc['StaticMesh'], 'StaticMesh')
            if mesh:
                smc.set_static_mesh(mesh)
            for i, mat in enumerate(mc.get('OverrideMaterials') or []):
                mo = load_asset(mat) if mat else None
                if mo:
                    smc.set_material(i, mo)
            smc.set_collision_profile_name('OverlapAllDynamic')
    return act


# ------------------------------------------------------------------ scenarios + world settings (best effort)
def _struct_types(*keywords):
    out = []
    for n in dir(unreal):
        if all(k.lower() in n.lower() for k in keywords):
            t = getattr(unreal, n, None)
            if isinstance(t, type) and issubclass(t, unreal.StructBase):
                out.append(t)
    return out


def set_struct_array(obj, prop, rows, keywords):
    """Set an array-of-structs property without knowing the struct's Python name."""
    errors = []
    types = _struct_types(*keywords)
    try:  # an existing element tells us the exact type
        cur = obj.get_editor_property(prop)
        if len(cur):
            types = [type(cur[0])] + [t for t in types if t is not type(cur[0])]
    except Exception as ex:
        errors.append('read %s: %s' % (prop, ex))
    # exact struct names seen in the Sandstorm SDK go first
    known = {'objectives': 'ObjectiveInfo', 'default_scenarios': 'DefaultScenarios'}.get(prop)
    if known and getattr(unreal, known, None) is not None:
        types = [getattr(unreal, known)] + [t for t in types if t is not getattr(unreal, known)]

    def make(T, row):
        # some Sandstorm struct fields are "not editable on instances", which blocks set_editor_property;
        # constructor keywords / plain attribute assignment take a different path
        try:
            return T(**row)
        except Exception:
            pass
        st = T()
        try:
            for k, v in row.items():
                try:
                    setattr(st, k, v)
                except Exception:
                    st.set_editor_property(k, v)
            return st
        except Exception:
            if not hasattr(st, 'import_text'):
                raise
        # last resort: the struct's text form, which skips the editor's edit-on-instance check
        def txt(v):
            if isinstance(v, (list, tuple)):
                return '(%s)' % ','.join(txt(x) for x in v)
            if isinstance(v, bool):
                return 'True' if v else 'False'
            if isinstance(v, unreal.Object):
                return '"%s"' % v.get_path_name()
            return '"%s"' % v
        st = T()
        st.import_text('(%s)' % ','.join('%s=%s' % (''.join(w.capitalize() for w in k.split('_')), txt(v))
                                          for k, v in row.items()))
        for k, v in row.items():
            if v and not st.get_editor_property(k):
                raise ValueError('text import left %s empty' % k)
        return st

    for T in types:
        try:
            items = [make(T, row) for row in rows]
            obj.set_editor_property(prop, items)
            return True
        except Exception as ex:
            errors.append('%s: %s' % (T.__name__, str(ex).splitlines()[0][:160]))
    # the first few are the likely struct types (the exact one first); the rest are guesses and only bloat the log
    log('could not set %s from Python; struct types tried: %s%s' % (
        prop, ' | '.join(errors[:3]) or 'no struct types matched %s' % (keywords,),
        ' (+%d other types)' % (len(errors) - 3) if len(errors) > 3 else ''))
    return False


def find_level_actor(full_ref):
    """'/Mod/Maps/Lvl.Lvl:PersistentLevel.SZ_A' -> the spawned actor labelled SZ_A in level Lvl."""
    if ':PersistentLevel.' not in full_ref:
        return None
    lvl = short(remap(full_ref.split(':')[0]))  # the level's name in the rebuilt mod
    name = full_ref.split(':PersistentLevel.')[1].split('.')[0]
    for a in EL.get_all_level_actors():
        if a.get_actor_label() == name:
            try:
                if a.get_outer().get_outer().get_name() != lvl:
                    continue
            except Exception:
                pass
            return a
    return None


def why_missing(refs, found, ex=None):
    """For the log: which referenced actors weren't found, and whether their level is even loaded."""
    missing = [r for r, a in zip(refs, found) if a is None]
    if not missing:
        return str(ex).splitlines()[0][:200] if ex else 'all actors found, setting the list failed'
    loaded = set()
    labels = {}
    for a in EL.get_all_level_actors():
        try:
            lv = a.get_outer().get_outer().get_name()
        except Exception:
            continue
        loaded.add(lv)
        labels.setdefault(a.get_actor_label(), lv)
    out = []
    for r in missing[:8]:
        lvl = short(r.split(':')[0])
        name = r.split(':PersistentLevel.')[-1].split('.')[0]
        if lvl not in loaded:
            out.append('%s (level %s is not loaded)' % (name, lvl))
        elif name in labels:
            out.append('%s (found in %s instead)' % (name, labels[name]))
        else:
            out.append('%s (no actor with that label in %s)' % (name, lvl))
    return '%d of %d not found: %s' % (len(missing), len(refs), ', '.join(out))


def resolve_cross_refs(persistents):
    """Properties pointing at an actor in another sublevel (spawn zones -> objectives in a scenario level).
    Both levels are loaded together under their persistent level, then the reference is set and saved."""
    todo_refs = list(cross_refs)
    for p in persistents:
        if not todo_refs:
            break
        EL.load_level(remap(p))
        left = []
        for lvl, label, comp, pname, ref, ref_lvl in todo_refs:
            owner = find_level_actor('%s.%s:PersistentLevel.%s' % (lvl, short(lvl), label))
            refs = ref if isinstance(ref, list) else [ref]
            targets = [find_level_actor('%s.%s:PersistentLevel.%s' % (ref_lvl, short(ref_lvl), x)) for x in refs]
            target = targets if isinstance(ref, list) else targets[0]
            if owner is None or any(t is None for t in targets):
                left.append((lvl, label, comp, pname, ref, ref_lvl))
                continue
            obj = owner
            if comp:
                obj = components_by_name(owner).get(comp)
            try:
                assign(obj, pname, target)
            except Exception as ex:
                log('cross-level ref %s.%s -> %s: %s' % (label, pname, ref, ex))
        todo_refs = left
        EL.save_all_dirty_levels()
    for lvl, label, comp, pname, ref, ref_lvl in todo_refs:
        todo('%s (%s): set %s to %s from %s' % (label, short(lvl), pname, ref, short(ref_lvl)))
    log('cross-level references: %d set, %d left to do by hand' % (len(cross_refs) - len(todo_refs), len(todo_refs)))


def lighting_key(level_name, persistent_name):
    low = level_name.lower()
    for k in ('Day', 'Night', 'Dusk', 'Dawn'):
        if k.lower() in low:
            return k
    return level_name.replace(persistent_name + '_', '')


def set_scenario_name(asset, val):
    """UI Display Name (ScenarioName): the label under the map name on the vote card and in the server browser
    ("Security", "Insurgents"). The original either has plain text or a line of the game's string table
    (ST_Scenarios); a string-table line is linked so it stays translated, else its key (the English text) is used."""
    text = None
    if isinstance(val, dict):
        if not val.get('key'):
            return False
        lib = getattr(unreal, 'TextLibrary', None) or getattr(unreal, 'KismetTextLibrary', None)
        fn = getattr(lib, 'text_from_string_table', None) if lib else None
        if fn and val.get('string_table'):
            try:
                text = fn(unreal.Name(val['string_table']), val['key'])
                if not str(text) or 'MISSING' in str(text).upper():
                    text = None
            except Exception:
                text = None
        if text is None:
            text = unreal.Text(val['key'])
    else:
        text = unreal.Text(str(val))
    for prop in ('scenario_name', 'ui_display_name'):
        try:
            asset.set_editor_property(prop, text)
            return True
        except Exception:
            continue
    return False


def create_scenarios(layout, lighting, persistent):
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    made = []
    for pkg, s in (layout.get('scenarios') or {}).items():
        # only the scenarios that play on the level that is open: their objectives are in its sublevels, and
        # running the rest here (TORO also has Assault_Backup_Gamemodes) reported every link as failed
        if s.get('Level') and s['Level'].split('.')[0] != persistent:
            continue
        dest = remap(pkg)
        if EAL.does_asset_exist(dest):
            # refresh it: the rebuild respawns the actors, so the objectives/spawn zones it pointed at are gone
            asset = unreal.load_asset(dest)
        else:
            cls = getattr(unreal, s.get('class', ''), None)
            if cls is None:
                todo('scenario %s: class %s not exposed to Python; create it by hand' % (short(dest), s.get('class')))
                continue
            try:
                fac = unreal.DataAssetFactory()
                fac.set_editor_property('data_asset_class', cls)
                asset = tools.create_asset(short(dest), dest.rsplit('/', 1)[0], cls, fac)
            except Exception as ex:
                todo('scenario %s could not be created (%s); create it by hand' % (short(dest), ex))
                continue
        if asset is None:
            continue
        fails = []
        simple = {'Map': renamed_short(s.get('Map')), 'AttackingTeam': s.get('AttackingTeam')}
        apply_props(asset, {k: v for k, v in simple.items() if isinstance(v, (str, int))})
        shown = s.get('ScenarioName') or s.get('UIDisplayName')
        if shown and not set_scenario_name(asset, shown):
            fails.append('UI Display Name = %s' % (shown.get('key') if isinstance(shown, dict) else shown))
        for key, val, loader in (('level', s.get('Level'), load_asset), ('game_mode', s.get('GameMode'), load_class),
                                 ('default_theater', s.get('DefaultTheater'), load_asset)):
            if not val:
                continue
            obj = loader(val)
            if obj is None:
                fails.append('%s = %s (not found%s)' % (key, val, '; custom Blueprint game mode, rebuild it or pick a '
                                                       'stock mode' if key == 'game_mode' and not val.startswith('/Script/') else ''))
                continue
            try:
                asset.set_editor_property(key, obj)
            except Exception as ex:
                log('scenario %s: %s: %s' % (short(dest), key, str(ex).splitlines()[0][:200]))
                fails.append(key)
        subs = [{'name': renamed_short(x.get('Name', '')), 'load_on_server': bool(x.get('bLoadOnServer', True)),
                 'load_on_client': bool(x.get('bLoadOnClient', False))}
                for x in (s.get('Sublevels') or []) if isinstance(x, dict)]
        if subs and not set_struct_array(asset, 'sublevels', subs, 'Sublevel'):
            fails.append('Sublevels (%s)' % ', '.join(x['name'] for x in subs))
        zones = [find_level_actor(z) for z in (s.get('NeutralSpawnZones') or [])]
        if zones:
            try:
                if None in zones:
                    raise ValueError('zone actor not found')
                asset.set_editor_property('neutral_spawn_zones', zones)
            except Exception as ex:
                log('scenario %s: neutral spawn zones: %s' % (short(dest), why_missing(s['NeutralSpawnZones'], zones, ex)))
                fails.append('Neutral Spawn Zones (%s)' % ', '.join(short(z.split('.')[-1]) for z in s['NeutralSpawnZones']))
        objs = []
        for o in (s.get('Objectives') or []):
            if not isinstance(o, dict) or not o.get('Objective'):
                continue
            act = find_level_actor(o['Objective'])
            sz = [find_level_actor(z) for z in (o.get('SpawnZones') or [])]
            objs.append((o, act, sz))
        if objs:
            ok = all(a is not None and None not in sz for _, a, sz in objs) and set_struct_array(
                asset, 'objectives', [{'objective': a, 'spawn_zones': sz} for _, a, sz in objs], 'Objective')
            if not ok:
                refs = [o['Objective'] for o, _, _ in objs] + [z for o, _, _ in objs for z in (o.get('SpawnZones') or [])]
                found = [a for _, a, _ in objs] + [z for _, _, sz in objs for z in sz]
                log('scenario %s: objectives: %s' % (short(dest), why_missing(refs, found)))
                fails.append('Objectives in this order: %s' % ', '.join(
                    o['Objective'].split('.')[-1] for o, _, _ in objs))
        # Outpost: objectives come in groups (each group: its objectives + the spawn zones used while it is active)
        groups = []
        for g in (s.get('ObjectiveGroups') or []):
            if not isinstance(g, dict) or not g.get('Objectives'):
                continue
            acts = [find_level_actor(o) for o in g['Objectives'] if isinstance(o, str)]
            sz = [find_level_actor(z) for z in (g.get('SpawnZones') or []) if isinstance(z, str)]
            groups.append((g, acts, sz))
        if groups:
            ok = all(None not in acts and None not in sz for _, acts, sz in groups) and set_struct_array(
                asset, 'objective_groups', [{'objectives': acts, 'spawn_zones': sz} for _, acts, sz in groups],
                'ObjectiveGroup')
            if not ok:
                refs = [o for g, _, _ in groups for o in g['Objectives']] + [z for g, _, _ in groups for z in (g.get('SpawnZones') or [])]
                found = [a for _, acts, _ in groups for a in acts] + [z for _, _, sz in groups for z in sz]
                log('scenario %s: objective groups: %s' % (short(dest), why_missing(refs, found)))
                fails.append('Objective Groups in this order: %s' % '; '.join(
                    ', '.join(o.split('.')[-1] for o in g['Objectives']) for g, _, _ in groups))
            objs += [(None, a, []) for _, acts, _ in groups for a in acts]  # for the count below
        if lighting:
            want = s.get('DefaultLightingScenario')
            key = next(iter(lighting))
            if isinstance(want, str) and want:
                key = next((k for k, v in lighting.items() if short(v) == want or k == want), key)
            try:
                asset.set_editor_property('default_lighting_scenario', key)
            except Exception:
                fails.append('Default Lighting Scenario = %s' % key)
        EAL.save_loaded_asset(asset)
        log('scenario %s: %d objectives linked' % (dest, len(objs)))
        if fails:
            todo('scenario %s: set by hand -> %s' % (short(dest), '; '.join(fails)))
        made.append((s, asset))
    return made


def world_settings(layout, persistent, scenarios, lighting):
    ws = EL.get_editor_world().get_world_settings()
    # default scenarios
    rows = []
    for s, asset in scenarios:
        if asset is None or not s.get('GameMode') or s.get('Level', '').split('.')[0] != persistent:
            continue
        gm = load_class(s['GameMode'])
        rows.append({'game_mode': gm, 'scenario': asset})
    if rows and not set_struct_array(ws, 'default_scenarios', rows, 'Scenario'):
        # Python isn't allowed to set GameMode in this struct, but the Details panel pastes text fine
        text = '(%s)' % ','.join('(GameMode=Class\'"%s"\',Scenario=%s\'"%s"\')' % (
            r['game_mode'].get_path_name() if r['game_mode'] else 'None',
            r['scenario'].get_class().get_name(), r['scenario'].get_path_name()) for r in rows)
        # one file per map when the mod has several, or each map's line would overwrite the one before it.
        # The clipboard holds one line, so only the main map's goes there.
        maps = used_persistents(layout, note=False)
        several = len(maps) > 1
        fname = 'default_scenarios_%s.txt' % short(remap(persistent)) if several else 'default_scenarios.txt'
        with open(os.path.join(HERE, fname), 'w', encoding='utf-8') as fh:
            fh.write(text)
        on_clip = (not maps or persistent == maps[0]) and set_clipboard(text)
        todo('World Settings > Default Scenarios%s (%d): copy the line in %s (next to this script%s), '
             'then right-click the Default Scenarios heading in World Settings > Paste' % (
                 ' of %s' % short(remap(persistent)) if several else '', len(rows), fname,
                 '; it is also on the clipboard' if on_clip else ''))
    # lighting scenarios
    if lighting:
        done = False
        for make in (lambda k, w: {unreal.Name(k): w}, lambda k, w: {k: w}, lambda k, w: {k: w.get_name()}):
            try:
                m = {}
                for k, lp in lighting.items():
                    m.update(make(k, load_asset(lp + '.' + short(lp), 'World')))
                ws.set_editor_property('lighting_scenarios', m)
                ws.set_editor_property('default_lighting_scenario', next(iter(lighting)))
                done = True
                break
            except Exception:
                continue
        if not done:
            todo('World Settings > Lighting Scenarios: add %s, Default Lighting Scenario = %s' % (
                ', '.join('%s -> %s' % (k, short(v)) for k, v in lighting.items()), next(iter(lighting))))
    # tactical map
    mats = [remap(p) for p, m in (layout.get('materials') or {}).items() if 'MapOverview' in (m.get('Parent') or '')]
    tm = (layout['levels'].get(persistent, {}).get('world_settings') or {}).get('TacticalMapMaterial')
    target = remap(tm) if tm else (mats[0] if mats else None)
    if target:
        mat = unreal.load_asset(target)
        if mat is None:
            todo('World Settings > Tactical Map Material: %s does not exist. Make an Unlit material at that path '
                 'with the minimap texture in Emissive Color (extracts from r5 on make a stand-in for it), then run '
                 'Re-link scenarios only.' % target)
        else:
            try:
                ws.set_editor_property('tactical_map_material', mat)
            except Exception:
                todo('World Settings > Tactical Map Material = ' + target)
    EL.save_all_dirty_levels()


# ------------------------------------------------------------------ main
SINGLE_MODES = ('slots', 'scenarios', 'lods', 'graft', 'graftpak', 'merge', 'roads', 'foliage', 'holes')


def main():
    global OLD_ROOT, NEW_MOD_ROOT
    layout = json.load(open(LAYOUT, encoding='utf-8'))
    log('rebuild script %s, extract made by %s %s' % (BUILD, layout.get('tool_version', '?'), layout.get('tool_build', '(before r19)')))
    OLD_ROOT = '/' + layout['mods'][0] if layout.get('mods') else None
    if NEW_MOD_ROOT is None and layout.get('target_mod'):
        NEW_MOD_ROOT = '/' + layout['target_mod']
    if NEW_MOD_ROOT and NEW_MOD_ROOT != OLD_ROOT:
        # only the maps the scenarios play on, and the scenarios, change name (extracts before r30 also listed
        # sublevels such as TORO_Lights here; those keep their names, so they are left out)
        keep = set(layout.get('scenarios') or {}) | {(s.get('Level') or '').split('.')[0]
                                                     for s in (layout.get('scenarios') or {}).values()
                                                     if isinstance(s, dict)}
        ren = layout.get('renames') or {}
        RENAMES.update({k: v for k, v in ren.items() if k in keep} if keep - {''} else ren)
        SHORT_RENAMES.update({k.rsplit('/', 1)[-1]: v.rsplit('/', 1)[-1] for k, v in RENAMES.items()})
        maps = ['%s -> %s' % (short(k), short(v)) for k, v in RENAMES.items() if '/Scenarios/' not in k
                and k not in (layout.get('scenarios') or {})]
        log('rebuilding into mod %s (original %s)%s' % (NEW_MOD_ROOT, OLD_ROOT,
                                                       '; maps: ' + ', '.join(maps) if maps else ''))
        if not _mod_exists(NEW_MOD_ROOT):
            log('STOPPED: there is no mod named %s in this SDK. Create it (same name exactly), then run this again.'
                % NEW_MOD_ROOT.strip('/'))
            write_log()
            unreal.EditorDialog.show_message('Map rebuild', 'Create a mod named "%s" in the SDK first (exactly that '
                                             'name), then run Rebuild map again.' % NEW_MOD_ROOT.strip('/'),
                                             unreal.AppMsgType.OK)
            return
    elif OLD_ROOT:
        log('rebuilding under the original mod name %s: players who still have the original installed will load it '
            'instead of this one' % OLD_ROOT)
    RELOCATE.update(layout.get('relocate') or {})
    FALLBACK.update(layout.get('relocate_if_missing') or {})
    DEP_MODS[:] = layout.get('dep_mods') or []
    LAYOUT_DATA.update(layout)
    if NEW_MOD_ROOT == OLD_ROOT:
        NEW_MOD_ROOT = None
    args = [a.lower() for a in sys.argv[1:]]
    # textures and materials belong to a full rebuild (and the landscape pass, which uses them); the single-job
    # menu entries leave them alone instead of re-saving every material each time
    single = [m for m in SINGLE_MODES if m in args]
    if DO_TEXTURES_AND_MATERIALS and not single:
        import_textures(layout)
        repair_gloss_textures(layout)
        create_materials(layout)
    if 'slots' in args:
        # grey materials are also what an outdated stand-in parent looks like: bring any existing ones up to date
        for kind, (name, _) in STAND_IN_KINDS.items():
            if EAL.does_asset_exist(mod_root() + '/SMR/' + name):
                stand_in_parent(kind)
        n = 0
        for pkg, m in sorted((layout.get('meshes') or {}).items()):
            mesh = imported_mesh(pkg, m)
            if mesh is not None and assign_slots(mesh, m):
                EAL.save_loaded_asset(mesh)
                n += 1
        log('re-linked materials on %d of %d custom meshes' % (n, len(layout.get('meshes') or {})))
        finish()  # 'slots': only point the mod's meshes at their original materials
        return
    if 'scenarios' in args:
        relink_scenarios(layout)
        finish()  # 'scenarios': only re-point scenarios/World Settings at the actors in the level as it is
        return
    if 'lods' in args:
        n = 0
        with unreal.ScopedSlowTask(len(layout.get('meshes') or {}), 'Adding LODs') as task:
            task.make_dialog(True)
            for pkg, m in sorted((layout.get('meshes') or {}).items()):
                if task.should_cancel():
                    break
                task.enter_progress_frame(1, short(pkg))
                mesh = imported_mesh(pkg, m)
                if mesh is not None and add_lods(mesh, m):
                    EAL.save_loaded_asset(mesh)
                    n += 1
        log('added LODs to %d imported meshes' % n)
        finish()  # 'lods': only give the imported meshes LODs, levels untouched
        return
    if 'graft' in args:
        graft(layout)
        finish()  # 'graft': placeholders for the original mod's logic Blueprints + their placed actors
        return
    if 'graftpak' in args:
        build_graft_pak(layout)
        finish()  # 'graftpak': pack the original compiled Blueprints next to the cooked pak
        return
    if 'merge' in args:
        merge_props(layout)
        finish()  # 'merge': only turn repeated prop actors into instanced components
        return
    if 'roads' in args:
        rebuild_roads(layout)
        finish()  # 'roads': only (re)place the landscape spline meshes, leave every other actor alone
        return
    if 'foliage' in args:
        rebuild_foliage(layout)
        finish()  # 'foliage': only replace the foliage, leave every other actor alone
        return
    if 'holes' in args:
        apply_landscape_settings(layout)
        finish()  # 'holes': after the hand import, cut the holes and fill in what the panel can't carry
        return
    if 'landscape' in args:
        build_landscape_materials(layout)
        finish()  # 'py rebuild_map.py landscape': only (re)build the landscape material
        return
    FULL_RUN['on'] = not ONLY_LEVELS
    if DO_MESHES and not import_meshes(layout):
        finish()
        return
    # after the meshes: grass types point at the mod's own grass meshes, which only exist once imported
    build_landscape_materials(layout)
    if DO_BLUEPRINTS:
        build_blueprints(layout)
    snd = [x for x in layout.get('sounds') or [] if 'file' in x]
    if snd:
        todo('SOUNDS: the original audio is in the sounds folder next to this script (%s). Import each into Wwise '
             'and make the events %s, or point those actors at stock game sounds.' % (
                 ', '.join(x['file'].split('/')[-1] for x in snd),
                 ', '.join(sorted({short(e) for e in _missing_events(layout)}))))
    levels = layout['levels']
    persistents = used_persistents(layout)
    for p in persistents:
        if levels[p].get('level_script'):
            todo('%s has a level Blueprint with logic (%s). A persistent level is never grafted, so rebuild what it '
                 'did by hand in the new level Blueprint, or leave it out if it was cosmetic.' % (
                     short(p), ', '.join(levels[p]['level_script'])))
    order = level_order(levels, persistents)
    if ONLY_LEVELS:
        order = [l for l in order if short(l) in ONLY_LEVELS]
    run_across_ticks(_levels_and_rest(layout, levels, persistents, order))


def _levels_and_rest(layout, levels, persistents, order):
    prog = load_progress()
    for name in order:
        if name in prog['done']:
            replay_level(prog['done'][name])
            log('%s: already rebuilt by an earlier run that stopped early; skipped' % short(name))
            continue
        before = snapshot()
        yield from build_level(name, levels[name], prog, name)
        if CANCELLED['on']:
            FULL_RUN['on'] = False  # keep rebuild_progress.json so the next run resumes
            finish()
            return
        prog['done'][name] = since(before)
        prog.pop('partial', None)
        save_progress(prog)
        yield
    for p in persistents:
        if ONLY_LEVELS and short(p) not in ONLY_LEVELS:
            continue
        EL.load_level(remap(p))
        add_streaming(levels[p]['streaming'])
        lighting = lighting_for(levels, p)
        mark_lighting_scenarios(lighting)
        scen = create_scenarios(layout, lighting, p) if DO_SCENARIOS else []
        if DO_WORLD_SETTINGS:
            EL.load_level(remap(p))
            world_settings(layout, p, scen, lighting)
    if cross_refs:
        resolve_cross_refs(persistents)
    _rest_of_main(layout, levels)


def run_across_ticks(gen):
    """Step a generator once per editor frame until it finishes, so the editor completes frames (and frees their
    memory) during a long rebuild. The editor stays responsive meanwhile; leave it alone until the rebuild ends."""
    import traceback
    st = {'busy': False}

    def stop():
        sys.smr_running = False
        try:
            unreal.unregister_slate_post_tick_callback(st['handle'])
        except Exception:
            pass

    def tick(_dt):
        if st['busy']:
            return  # a progress dialog ticks the UI from inside a step; don't start another step then
        st['busy'] = True
        try:
            next(gen)
        except StopIteration:
            stop()
        except Exception:
            stop()
            log('rebuild stopped with an error:\n' + traceback.format_exc())
            write_log()
            unreal.EditorDialog.show_message('Map rebuild', 'The rebuild stopped with an error. Run Rebuild map '
                                             'again to resume; the error is at the end of rebuild_log.txt.',
                                             unreal.AppMsgType.OK)
        finally:
            st['busy'] = False

    if getattr(sys, 'smr_running', False):
        unreal.EditorDialog.show_message('Map rebuild', 'A rebuild is already running. Wait for it to finish.',
                                         unreal.AppMsgType.OK)
        return
    sys.smr_running = True
    log('placing actors across editor frames; leave the editor alone until the rebuild finishes')
    st['handle'] = unreal.register_slate_post_tick_callback(tick)


# ------------------------------------------------------------------ resume after a crash
PROGRESS = os.path.join(HERE, 'rebuild_progress.json')


def _layout_stamp():
    st = os.stat(LAYOUT)
    return '%d-%d' % (st.st_size, int(st.st_mtime))


def load_progress():
    """Levels finished by an earlier run of this same extract that stopped early (crash, out of memory).
    A full run deletes the file when it completes, so a normal re-run still rebuilds everything."""
    try:
        with open(PROGRESS) as fh:
            p = json.load(fh)
        if p.get('layout') == _layout_stamp() and p.get('root') == mod_root() and isinstance(p.get('done'), dict):
            if p['done']:
                log('resuming: %d level(s) were finished by the last run' % len(p['done']))
            return p
    except Exception:
        pass
    return {'layout': _layout_stamp(), 'root': mod_root(), 'done': {}}


def save_progress(p):
    try:
        with open(PROGRESS, 'w') as fh:
            json.dump(p, fh)
    except Exception as ex:
        log('could not save progress: %s' % ex)


def clear_progress():
    try:
        os.remove(PROGRESS)
    except OSError:
        pass


def snapshot():
    return {'cross': len(cross_refs), 'manual': len(manual), 'missing': len(MISSING_ASSETS),
            'foliage': len(FOLIAGE_LEFT),
            'classes': {k: len(v) for k, v in missing_classes.items()}}


def since(b):
    """What a level added to the run-wide lists, so a resumed run can put it back without rebuilding."""
    return {'cross': [list(x) for x in cross_refs[b['cross']:]], 'manual': manual[b['manual']:],
            'missing': MISSING_ASSETS[b['missing']:], 'foliage': FOLIAGE_LEFT[b.get('foliage', 0):],
            'classes': {k: v[b['classes'].get(k, 0):] for k, v in missing_classes.items()
                        if len(v) > b['classes'].get(k, 0)}}


def replay_level(d):
    cross_refs.extend(tuple(x) for x in d.get('cross') or [])
    manual.extend(d.get('manual') or [])
    FOLIAGE_LEFT.extend(d.get('foliage') or [])
    MISSING_ASSETS.extend(m for m in d.get('missing') or [] if m not in MISSING_ASSETS)
    for k, v in (d.get('classes') or {}).items():
        missing_classes.setdefault(k, []).extend(v)


def used_persistents(layout, note=True):
    """The persistent levels that are actual maps: the ones a scenario plays on. Mods often ship the author's
    leftover working maps too (test maps, a marketplace pack's demo level); those are skipped and listed."""
    scen = (layout.get('scenarios') or {}).values()
    played = {obj_path_of(s.get('Level')) for s in scen if s.get('Level')}
    levels = layout.get('levels') or {}
    # a level a scenario plays on is the map, even if an older extract listed it as a sublevel (an empty wrapper
    # level that streams the real map demotes it; extracts from r4 on already count it as persistent)
    allp = list(layout['persistent_levels']) + [p for p in played if p in levels and p not in layout['persistent_levels']]
    # scenario sublevels (game-mode layers) are loaded by the scenario, not streamed, so they look persistent too
    subs = {(x.get('Name') or '').lower() for s in scen for x in (s.get('Sublevels') or []) if isinstance(x, dict)}
    # lighting layers (Day/Night): the ones the played maps' World Settings name; if no map names any, fall back
    # to "has a sun or sky light", so a demo map that happens to carry its own lights isn't mistaken for one
    ls_names = {str(v).lower() for p in played for v in
                ((levels.get(p, {}).get('world_settings') or {}).get('LightingScenarios') or {}).values()}
    if ls_names:
        lit = {p for p in allp if short(p).lower() in ls_names}
    else:
        lit = {p for p in allp if any(a['class'].split('.')[-1] in ('DirectionalLight', 'SkyLight')
                                      for a in levels.get(p, {}).get('actors', []))}
    used = [p for p in allp if p in played or short(p).lower() in subs or p in lit]
    if not used:
        return allp
    used.sort(key=lambda p: p not in played)
    skipped = [p for p in allp if p not in used]
    if skipped and note:
        todo('Not rebuilt (no scenario uses them, likely the author\'s working maps): %s. Rebuild one with '
             'ONLY_LEVELS if you need it.' % ', '.join(short(p) for p in skipped))
    return used


def lighting_for(levels, p):
    """Lighting scenario name -> level for persistent level p, as the original map had them."""
    lighting = {}
    ws0 = levels[p].get('world_settings') or {}
    if isinstance(ws0.get('LightingScenarios'), dict) and ws0['LightingScenarios']:
        # use the original map's own lighting scenario names (default one first)
        byshort = {short(l): l for l in levels}
        items = sorted(ws0['LightingScenarios'].items(), key=lambda kv: kv[0] != ws0.get('DefaultLightingScenario'))
        lighting = {k: remap(byshort.get(short(str(v)), str(v))) for k, v in items}
    for s in ([] if lighting else levels[p]['streaming']):
        d = levels.get(s['level'], {})
        if s['type'] != 'LevelStreamingAlwaysLoaded' and any(
                a['class'].split('.')[-1] in LIGHT_CLASSES for a in d.get('actors', [])):
            lighting[lighting_key(short(s['level']), short(p))] = remap(s['level'])
    return lighting


def built_levels(layout):
    """The levels a full run rebuilds: the maps scenarios play on and their sublevels."""
    levels = layout.get('levels') or {}
    ps = used_persistents(layout, note=False)
    out = set(ps)
    for p in ps:
        out.update(x['level'] for x in levels.get(p, {}).get('streaming') or [])
    return out


def level_order(levels, persistents):
    keep = set(persistents)
    for p in persistents:
        keep.update(s['level'] for s in levels[p].get('streaming') or [])
    return [l for l in levels if l in keep and l not in persistents] + persistents


def rebuild_foliage(layout):
    if holder_class() is None:
        log('no %s anywhere in the project: make an Actor Blueprint with that name and a Hierarchical '
            'Instanced Static Mesh component (any folder; it gets copied into the mod)' % HOLDER_BP)
        return
    levels = layout['levels']
    for name in level_order(levels, used_persistents(layout, note=False)):
        if not levels[name].get('foliage') or not EAL.does_asset_exist(remap(name)) or not EL.load_level(remap(name)):
            continue
        CUR['level'] = remap(name)
        _foliage_actors['n'] = 0
        old = [a for a in EL.get_all_level_actors()
               if TAG in [str(t) for t in a.get_editor_property('tags')]
               and str(a.get_folder_path()).startswith('Foliage')]
        for a in old:
            EL.destroy_actor(a)
        for i, f in enumerate(levels[name]['foliage']):
            build_foliage(f, i)
        EL.save_current_level()
        want = sum(len(f['instances']) for f in levels[name]['foliage'])
        EL.load_level(remap(name))  # check what actually got saved, not what is in memory
        got = sum(c.get_instance_count() for a in EL.get_all_level_actors()
                  if str(a.get_folder_path()) == 'Foliage'
                  for c in [a.get_component_by_class(unreal.HierarchicalInstancedStaticMeshComponent)] if c)
        log('%s: foliage replaced (%d old actors removed, %d of %d instances saved)' % (
            short(name), len(old), got, want))
        _asset_cache.clear()
        _class_cache.clear()
        try:
            unreal.SystemLibrary.collect_garbage()
        except Exception:
            pass


MERGE_MIN = 3  # a mesh placed at least this many times in one level becomes one instanced component


def _merge_key(a):
    comp = a.get_editor_property('static_mesh_component')
    mesh = comp.static_mesh if comp else None
    if mesh is None or comp.get_editor_property('mobility') != unreal.ComponentMobility.STATIC:
        return None, None
    mats = tuple(m.get_path_name() if m else '' for m in (comp.get_editor_property('override_materials') or []))
    if not _instancing_ok(comp.get_materials()):
        return None, None
    key = (mesh.get_path_name(), mats, str(comp.get_collision_profile_name()),
           bool(comp.get_editor_property('cast_shadow')), float(comp.get_editor_property('ld_max_draw_distance')))
    return key, comp


def _instancing_ok(mats):
    """A material can only draw on an instanced component if its base material has 'Used with Instanced Static
    Meshes' ticked. The editor ticks it by itself as soon as such a component uses the material, but it can only
    save that for the mod's own materials. The game's /Game materials can't be changed from a mod, so in the cooked
    game an instanced copy of one falls back to the default material (an early test of the merge turned TORO's
    water planes into grey ground that way): meshes using one without the flag stay separate actors.
    Never set the flag from here: changing a material while the level renders crashes the editor (an early test did)."""
    for m in mats or []:
        if m is None:
            continue
        try:
            base = m.get_base_material()
            if base is None or base.get_editor_property('used_with_instanced_static_meshes'):
                continue
            if base.get_path_name().startswith(('/Game/', '/Engine/')):
                return False
        except Exception:
            return False
    return True


def _save_merged_materials(level_pkg):
    """Save the mod's own base materials of every merged component, so the instanced usage the editor ticked on
    them goes into the cook."""
    HISM = unreal.HierarchicalInstancedStaticMeshComponent
    done = set()
    for act in EL.get_all_level_actors():
        if str(act.get_folder_path()) != 'Merged' or not act.get_path_name().startswith(level_pkg):
            continue
        comp = act.get_component_by_class(HISM)
        for m in (comp.get_materials() if comp else []):
            try:
                base = m.get_base_material() if m else None
                path = base.get_path_name() if base else ''
                if path and path not in done and not path.startswith(('/Game/', '/Engine/')):
                    done.add(path)
                    EAL.save_loaded_asset(base)
            except Exception:
                pass


def _mirrored(xf):
    sc = xf.scale3d
    return sc.x * sc.y * sc.z < 0


def _spawn_split(mesh, mats, profile, shadow, xf, label):
    a = EL.spawn_actor_from_class(unreal.StaticMeshActor, xf.translation, xf.rotation.rotator())
    a.set_actor_transform(xf, False, False)
    c = a.get_editor_property('static_mesh_component')
    c.set_mobility(unreal.ComponentMobility.STATIC)
    c.set_static_mesh(mesh)
    for mi, m in enumerate(mats):
        if m:
            c.set_material(mi, m)
    c.set_collision_profile_name(profile)
    c.set_cast_shadow(shadow)
    a.set_actor_label(label)
    a.set_editor_property('tags', [TAG])


def _unmerge_bad(level_pkg):
    """Undo the parts of an earlier merge that can't render instanced:
    - a material without 'Used with Instanced Static Meshes' (see _instancing_ok): the whole component goes back
      to separate actors;
    - mirrored copies (negative scale): an instanced component can't flip face culling per copy, so they render
      inside out (an early test left see-through cave rocks). Only those copies go back to separate actors."""
    HISM = unreal.HierarchicalInstancedStaticMeshComponent
    n = 0
    for act in EL.get_all_level_actors():
        if str(act.get_folder_path()) != 'Merged' or not act.get_path_name().startswith(level_pkg):
            continue
        comp = act.get_component_by_class(HISM)
        if comp is None:
            continue
        mesh, mats = comp.static_mesh, list(comp.get_materials())
        profile, shadow = comp.get_collision_profile_name(), comp.get_editor_property('cast_shadow')
        xfs = []
        for i in range(comp.get_instance_count()):
            r = comp.get_instance_transform(i, True)
            xf = r[-1] if isinstance(r, tuple) else r
            if xf is not None:
                xfs.append(xf)
        all_out = not _instancing_ok(mats)
        out = xfs if all_out else [x for x in xfs if _mirrored(x)]
        if not out:
            continue
        for i, xf in enumerate(out):
            _spawn_split(mesh, mats, profile, shadow, xf, '%s_split_%d' % (mesh.get_name(), i))
        n += len(out)
        keep = [] if all_out else [x for x in xfs if not _mirrored(x)]
        if len(keep) < MERGE_MIN:
            for i, xf in enumerate(keep):
                _spawn_split(mesh, mats, profile, shadow, xf, '%s_split_k%d' % (mesh.get_name(), i))
            EL.destroy_actor(act)
        else:
            comp.clear_instances()
            comp = act.get_component_by_class(HISM)
            try:
                comp.add_instances(keep, False)
            except TypeError:
                comp.add_instances(keep, False, True)
        log('%s: %d cop%s back to separate actors (%s)' % (
            act.get_actor_label() if keep and len(keep) >= MERGE_MIN else mesh.get_name(), len(out),
            'y' if len(out) == 1 else 'ies', 'material not instanceable' if all_out else 'mirrored'))
    return n


def merge_props(layout):
    """Every mesh the rebuild placed 3+ times as separate actors goes into one instanced component per level.
    Each separate actor is its own draw call (11k of them on TORO); an instanced component draws all its copies
    in a few. Same look, collision and shadows. Rebuild lighting afterwards."""
    if holder_class() is None:
        log('no %s anywhere in the project: make an Actor Blueprint with that name and a Hierarchical '
            'Instanced Static Mesh component (any folder; it gets copied into the mod)' % HOLDER_BP)
        return
    levels = layout['levels']
    HISM = unreal.HierarchicalInstancedStaticMeshComponent
    for name in level_order(levels, used_persistents(layout, note=False)):
        if not EAL.does_asset_exist(remap(name)) or not EL.load_level(remap(name)):
            continue
        CUR['level'] = remap(name)
        pkg = remap(name) + '.'
        _unmerge_bad(pkg)
        groups = {}
        for a in EL.get_all_level_actors():
            if not isinstance(a, unreal.StaticMeshActor) or not a.get_path_name().startswith(pkg):
                continue
            if TAG not in [str(t) for t in a.get_editor_property('tags')]:
                continue
            if str(a.get_folder_path()).startswith(('Foliage', 'Roads', 'Merged')):
                continue
            if _mirrored(a.get_actor_transform()):
                continue  # mirrored copies render inside out when instanced
            key, comp = _merge_key(a)
            if key:
                groups.setdefault(key, []).append((a, comp))
        groups = {k: v for k, v in groups.items() if len(v) >= MERGE_MIN}
        before = sum(len(v) for v in groups.values())
        made = 0
        with unreal.ScopedSlowTask(len(groups), 'Merging props in ' + short(name)) as task:
            task.make_dialog(True)
            for i, (key, members) in enumerate(sorted(groups.items())):
                if task.should_cancel():
                    break
                task.enter_progress_frame(1, short(key[0]))
                mesh = members[0][1].static_mesh
                act = spawn(holder_class(), (0, 0, 0), {}, (1, 1, 1),
                            'Merged_%s_%d' % (key[0].split('.')[-1], i), 'Merged')
                if act is None or act.get_component_by_class(HISM) is None:
                    if act:
                        EL.destroy_actor(act)
                    continue
                for c in (act.get_editor_property('root_component'), act.get_component_by_class(HISM)):
                    try:
                        c.set_mobility(unreal.ComponentMobility.STATIC)
                    except Exception:
                        pass
                comp = act.get_component_by_class(HISM)
                comp.set_static_mesh(mesh)
                for mi, m in enumerate(key[1]):
                    mat = load_asset(m) if m else None
                    if mat:
                        comp.set_material(mi, mat)
                comp.set_cast_shadow(key[3])
                comp.set_collision_profile_name(key[2])
                if key[4] > 0:
                    comp.set_cull_distances(0, int(key[4]))
                xf = [a.get_actor_transform() for a, _ in members]
                comp = act.get_component_by_class(HISM)
                try:
                    comp.add_instances(xf, False)
                except TypeError:
                    comp.add_instances(xf, False, True)
                comp = act.get_component_by_class(HISM)
                if comp.get_instance_count() != len(xf) or comp.static_mesh != mesh:
                    log('merge %s: holder has %d of %d, kept the separate actors' % (
                        short(key[0]), comp.get_instance_count(), len(xf)))
                    EL.destroy_actor(act)
                    continue
                for a, _ in members:
                    EL.destroy_actor(a)
                made += 1
        EL.save_current_level()
        _save_merged_materials(pkg)
        log('%s: %d prop actors merged into %d instanced components' % (short(name), before, made))
        _asset_cache.clear()
        _class_cache.clear()
        try:
            unreal.SystemLibrary.collect_garbage()
        except Exception:
            pass


def rebuild_roads(layout):
    levels = layout['levels']
    todo_lv = [n for n in level_order(levels, used_persistents(layout, note=False)) if levels[n].get('splines')]
    if not todo_lv:
        log('no landscape spline meshes in layout.json: extract the map again with the current extractor')
        return
    for name in todo_lv:
        if not EAL.does_asset_exist(remap(name)) or not EL.load_level(remap(name)):
            continue
        CUR['level'] = remap(name)
        old = [a for a in EL.get_all_level_actors()
               if TAG in [str(t) for t in a.get_editor_property('tags')] and str(a.get_folder_path()) == 'Roads']
        for a in old:
            EL.destroy_actor(a)
        n0 = stats['spawned']
        for i, sp in enumerate(levels[name]['splines']):
            build_spline(sp, i)
        EL.save_current_level()
        log('%s: %d of %d road/fence spline meshes placed' % (
            short(name), stats['spawned'] - n0, len(levels[name]['splines'])))
        _asset_cache.clear()
        _class_cache.clear()


# ------------------------------------------------------------------ graft: the original mod's compiled Blueprints
GRAFT_FOLDER = 'Graft'
RAW_EXTS = ('.umap', '.uasset', '.uexp', '.ubulk', '.uptnl')


def graft_classes(layout):
    """The mod's own Blueprints with graph logic that are placed in the levels. Python can't rebuild graphs, so
    these get an empty placeholder Blueprint at the same path; the original compiled files replace it in the
    cooked game (graft pak)."""
    bps = layout.get('blueprints') or {}
    root = (OLD_ROOT or '') + '/'
    out = {}
    for name, data in layout['levels'].items():
        for a in data['actors']:
            pkg = a['class'].split('.')[0]
            info = bps.get(pkg)
            if info and info.get('has_logic') and pkg.startswith(root):
                out.setdefault(pkg, (info, a))
    return out


def _var_type(v):
    if isinstance(v, dict) and 'actor_ref' in v:
        return 'Actor (Object Reference)'
    if isinstance(v, bool):
        return 'Boolean'
    if isinstance(v, (int, float)):
        return 'Float' if isinstance(v, float) else 'Integer'
    return 'Text'


def _placed_of(layout, pkg):
    return [a for d in layout['levels'].values() for a in d['actors'] if a['class'].split('.')[0] == pkg]


GRAFT_NEEDS = {}


def _graft_needs(layout, pkg, info, sample):
    """(root name, root class, {variable: type}) the placeholder needs so the placed copies keep their position
    and settings when the original Blueprint replaces it."""
    comps = sample.get('components') or {}
    root = [n for n, c in comps.items() if c.get('is_root')]
    native_root = info.get('parent', '').endswith(('Character', 'Pawn')) or not root
    want_root = None
    if root and not native_root and root[0] != 'DefaultSceneRoot':
        want_root = (root[0], comps[root[0]].get('class', '').split('.')[-1])
    cdo = info.get('cdo') or {}
    vars_ = {}
    for a in _placed_of(layout, pkg):
        for k, v in (a.get('props') or {}).items():
            if k in SKIP_PROPS or not k[0].isupper() or k in ('SpawnCollisionHandlingMethod',):
                continue
            if v is None or cdo.get(k, '' if isinstance(v, str) else False) == v:
                continue
            vars_.setdefault(k, _var_type(v))
    return want_root, vars_


def _check_graft_stub(pkg, act):
    """What's still missing on a placeholder, judged from a placed copy."""
    want_root, vars_ = GRAFT_NEEDS.get(pkg, (None, {}))
    hints = []
    rc = act.get_editor_property('root_component')
    if want_root and (rc is None or rc.get_name() != want_root[0]):
        hints.append('make a %s named "%s" the root component (add it, drag it onto the current root)' % (
            want_root[1], want_root[0]))
    missing = []
    for k, t in sorted(vars_.items()):
        try:
            act.get_editor_property(k)
        except Exception:
            missing.append('%s : %s' % (k, t))
    if missing:
        hints.append('add Instance Editable variables ' + ', '.join(missing))
    return hints


def make_graft_stubs(layout):
    made = []
    for pkg, (info, sample) in sorted(graft_classes(layout).items()):
        dest = remap(pkg)
        if not EAL.does_asset_exist(dest):
            parent = load_class(info['parent']) if info.get('parent') else None
            if parent is None:
                todo('placeholder %s: parent class %s not found' % (dest, info.get('parent')))
                continue
            if new_blueprint(dest, parent) is None:
                todo('placeholder %s could not be created' % dest)
                continue
            EAL.save_asset(dest)
            made.append(short(dest))
        GRAFT_NEEDS[pkg] = _graft_needs(layout, pkg, info, sample)
    _class_cache.clear()
    return made


def place_grafted(layout):
    classes = graft_classes(layout)
    for pkg, (info, sample) in classes.items():
        GRAFT_NEEDS.setdefault(pkg, _graft_needs(layout, pkg, info, sample))
    levels = layout['levels']
    total = 0
    checked = set()
    for name in level_order(levels, used_persistents(layout, note=False)):
        acts = [a for a in levels[name]['actors'] if a['class'].split('.')[0] in classes]
        if not acts or not EAL.does_asset_exist(remap(name)) or not EL.load_level(remap(name)):
            continue
        CUR['level'] = remap(name)
        pkg = remap(name) + '.'
        for x in EL.get_all_level_actors():
            if str(x.get_folder_path()) == GRAFT_FOLDER and x.get_path_name().startswith(pkg):
                EL.destroy_actor(x)
        spawned = {str(x.get_actor_label()): x for x in EL.get_all_level_actors() if x.get_path_name().startswith(pkg)}
        n = 0
        for a in acts:
            old = spawned.pop(a['name'], None)
            if old is not None and TAG in [str(t) for t in old.get_editor_property('tags')]:
                EL.destroy_actor(old)  # a full rebuild placed it once the placeholder class existed
            build_actor(a, spawned)
            act = spawned.get(a['name'])
            if act:
                act.set_folder_path(GRAFT_FOLDER)
                n += 1
                pkgc = a['class'].split('.')[0]
                if pkgc not in checked:
                    checked.add(pkgc)
                    hints = _check_graft_stub(pkgc, act)
                    if hints:
                        todo('placeholder %s: %s, then compile and save' % (short(pkgc), '; '.join(hints)))
        for obj, pname, ref in deferred_refs:
            refs = ref if isinstance(ref, list) else [ref]
            found = [spawned[x] for x in refs if x in spawned]
            if not found:
                log('unresolved ref %s -> %s' % (pname, ', '.join(refs)))
                continue
            try:
                assign(obj, pname, found if isinstance(ref, list) else found[0])
            except Exception as ex:
                log('ref %s -> %s: %s' % (pname, ref, ex))
        del deferred_refs[:]
        EL.save_current_level()
        total += n
        log('%s: %d of %d original-Blueprint actors placed (folder %s)' % (short(name), n, len(acts), GRAFT_FOLDER))
    return total


def graft(layout):
    made = make_graft_stubs(layout)
    if made:
        log('placeholder Blueprints created: ' + ', '.join(made))
    place_grafted(layout)
    todo('after cooking: SMR > %s > Build graft pak, so the original Blueprints replace the placeholders' % menu_name())


def rename_package_names(data, rewrite):
    """Rewrite names in a cooked package header (.uasset/.umap, UE4.27 versioned cook).

    rewrite(name) returns a new name or None. Every name-table entry is passed through it; the table is rebuilt and
    every header offset that lies after it (and each export's SerialOffset, which counts from the start of the
    .uasset) is moved by the size difference. The .uexp/.ubulk files are unchanged. Returns (bytes, changed names).
    """
    import struct

    class Rd:
        def __init__(s, b):
            s.b, s.p = b, 0

        def i32(s):
            v = struct.unpack_from('<i', s.b, s.p)[0]
            s.p += 4
            return v

        def u32(s):
            v = struct.unpack_from('<I', s.b, s.p)[0]
            s.p += 4
            return v

        def i64(s):
            v = struct.unpack_from('<q', s.b, s.p)[0]
            s.p += 8
            return v

        def skip(s, n):
            s.p += n

        def fstr(s):
            n = s.i32()
            if n == 0:
                return ''
            if n > 0:
                v = s.b[s.p:s.p + n - 1].decode('latin-1')
                s.p += n
            else:
                v = s.b[s.p:s.p - n * 2 - 2].decode('utf-16-le')
                s.p += -n * 2
            return v

    r = Rd(data)
    if r.u32() != 0x9E2A83C1:
        raise ValueError('not a cooked package')
    legacy = r.i32()
    if legacy != -4:
        r.i32()
    ver = r.i32() or 522
    r.i32()
    for _ in range(r.i32()):
        r.skip(20)
    fields = {}  # name -> (pos, size)
    fields['TotalHeaderSize'] = (r.p, 4)
    r.i32()
    r.fstr()
    flags = r.u32()
    if flags & 0x2000:
        raise ValueError('unversioned package')
    name_count = r.i32()
    fields['NameOffset'] = (r.p, 4)
    name_off = r.i32()
    if ver >= 516 and not (flags & 0x80000000):
        r.fstr()
    if ver >= 459:
        r.i32()
        fields['GatherableTextDataOffset'] = (r.p, 4)
        r.i32()
    export_count = r.i32()
    fields['ExportOffset'] = (r.p, 4)
    export_off = r.i32()
    r.i32()
    fields['ImportOffset'] = (r.p, 4)
    r.i32()
    fields['DependsOffset'] = (r.p, 4)
    r.i32()
    if ver >= 384:
        r.i32()
        fields['SoftPackageReferencesOffset'] = (r.p, 4)
        r.i32()
    if ver >= 510:
        fields['SearchableNamesOffset'] = (r.p, 4)
        r.i32()
    fields['ThumbnailTableOffset'] = (r.p, 4)
    r.i32()
    r.skip(16)
    for _ in range(r.i32()):
        r.skip(8)
    for _ in range(2 if ver >= 444 else 1):
        r.skip(10)
        r.fstr()
    r.u32()
    if r.i32() != 0:
        raise ValueError('compressed package')
    r.u32()
    for _ in range(r.i32()):
        r.fstr()
    if legacy > -7:
        r.i32()
    fields['AssetRegistryDataOffset'] = (r.p, 4)
    r.i32()
    fields['BulkDataStartOffset'] = (r.p, 8)
    r.i64()
    if ver >= 224:
        fields['WorldTileInfoDataOffset'] = (r.p, 4)
        r.i32()
    if ver >= 326:
        for _ in range(r.i32()):
            r.i32()
    if ver >= 507:
        r.i32()
        fields['PreloadDependencyOffset'] = (r.p, 4)
        r.i32()

    # name table
    r.p = name_off
    entries, changed = [], []
    for _ in range(name_count):
        s = r.fstr()
        hashes = data[r.p:r.p + 4] if ver >= 504 else b''
        r.skip(len(hashes))
        new = rewrite(s)
        if new is not None and new != s:
            changed.append((s, new))
            s = new
        entries.append((s, hashes))
    name_end = r.p
    if not changed:
        return data, []
    out = bytearray()
    for s, hashes in entries:
        try:
            raw = s.encode('latin-1') + b'\0'
            out += struct.pack('<i', len(raw)) + raw
        except UnicodeEncodeError:
            raw = s.encode('utf-16-le') + b'\0\0'
            out += struct.pack('<i', -(len(raw) // 2)) + raw
        out += hashes
    delta = len(out) - (name_end - name_off)
    new = bytearray(data[:name_off]) + out + bytearray(data[name_end:])

    def bump(name):
        if name not in fields:
            return
        pos, size = fields[name]
        fmt = '<i' if size == 4 else '<q'
        v = struct.unpack_from(fmt, new, pos)[0]
        if name == 'TotalHeaderSize' or (v > 0 and v >= name_end) or (name == 'BulkDataStartOffset' and v > 0):
            struct.pack_into(fmt, new, pos, v + delta)
    for f in fields:
        if f != 'NameOffset':
            bump(f)
    # exports: SerialOffset counts from the start of the .uasset
    entry = 16 + 8 + 4 + 8 + 8 + 12 + 16 + 4 + (4 if ver >= 365 else 0) + (4 if ver >= 485 else 0) + \
        (20 if ver >= 507 else 0) - (0 if ver >= 508 else 4)
    off_in_entry = (16 if ver >= 508 else 12) + 8 + 4 + 8
    base = export_off + delta if export_off >= name_end else export_off
    for i in range(export_count):
        pos = base + i * entry + off_in_entry
        v = struct.unpack_from('<q', new, pos)[0]
        struct.pack_into('<q', new, pos, v + delta)
    return bytes(new), changed


def _graft_renamed_copies(entries, src, patched_dir, old_mod):
    """The original compiled files have the original's paths in their name tables (/TORO/...). For a renamed mod,
    rewrite those to the new mod's paths (and renamed level names) in copies of the .uasset/.umap headers; the
    .uexp/.ubulk data is untouched and packed as it is."""
    import shutil

    def rewrite(name):
        if name == '/' + old_mod or name.startswith('/' + old_mod + '/') or name.startswith('/' + old_mod + '.'):
            return remap(name)
        return SHORT_RENAMES.get(name)
    shutil.rmtree(patched_dir, ignore_errors=True)
    out, n_files, n_names = [], 0, 0
    for a, b in entries:
        if not a.lower().endswith(('.uasset', '.umap')):
            out.append((a, b))
            continue
        try:
            with open(a, 'rb') as fh:
                data, changed = rename_package_names(fh.read(), rewrite)
        except Exception as ex:
            log('graft pak: could not rewrite paths in %s (%s); stopped, nothing packed' % (a, ex))
            return None
        if not changed:
            out.append((a, b))
            continue
        dst = os.path.join(patched_dir, os.path.relpath(a, src))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as fh:
            fh.write(data)
        out.append((dst, b))
        n_files += 1
        n_names += len(changed)
    log('graft pak: original files moved to the renamed mod: %d names rewritten in %d files' % (n_names, n_files))
    return out


def build_graft_pak(layout):
    """Pack the original cooked files the extractor kept in graft/ (the scripted Blueprints and everything they
    reference) into <Mod>pakchunk1-WindowsClient_P.pak next to the cooked pak. _P makes it win over the
    placeholders cooked into pakchunk0. Files the rebuilt mod has itself are left out, and so are /Game/ files the
    game already has, so nothing of the base game is replaced."""
    import subprocess
    mod = (OLD_ROOT or '').strip('/')         # the original's folder inside graft/
    new_mod = (mod_root() or '').strip('/')   # the mod being shipped
    renamed = new_mod != mod or bool(RENAMES)
    src = os.path.join(HERE, 'graft')
    patched_dir = os.path.join(HERE, 'graft_renamed')
    if not os.path.isdir(src):
        log('no graft folder in %s: extract the map again with this version of the app, then run this again' % HERE)
        return
    stubs = {remap(p) for p in graft_classes(layout)}
    gameplay = ('INSPlayerStart', 'SpawnZone', 'SpawnZoneCounterAttack', 'CaptureZone', 'ObjectiveCapturable',
                'ObjectiveDestructible', 'ObjectiveWeaponCache', 'Obj_WeaponCache_Ins_C', 'Obj_WeaponCache_Sec_C',
                'INSPatrolArea', 'RestrictedArea')
    levels = set()
    maps = set(used_persistents(layout, note=False))
    for p in layout.get('graft_levels') or []:
        acts = (layout['levels'].get(p) or {}).get('actors') or []
        if p in maps:
            log('graft pak: %s is the map itself and is never replaced; its level Blueprint has to be rebuilt by '
                'hand' % short(p))
        elif any(a['class'].split('.')[-1] in gameplay for a in acts):
            log('graft pak: %s keeps the rebuilt level (it holds spawns or objectives); its level Blueprint has to be '
                'rebuilt by hand' % short(p))
        else:
            levels.add(p)
    stubs |= {remap(p) for p in levels} | {remap(p) + '_BuiltData' for p in levels}
    entries, game_files, skipped = [], set(), set()
    for dp, _, fs in os.walk(src):
        for f in fs:
            if not f.lower().endswith(RAW_EXTS):
                continue
            rel = os.path.relpath(os.path.join(dp, f), src).replace('\\', '/')
            if '/' not in rel:
                continue  # not under a content root
            root, rest = rel.split('/', 1)
            pkg = '/%s/%s' % (root, os.path.splitext(rest)[0])
            if root == 'Game':
                # shipped by the original under /Game/ (e.g. a marketplace pack at its install path)
                if EAL.does_asset_exist(pkg):
                    continue  # the game has it: never replace base-game files
                entries.append((os.path.join(dp, f), '../../../Insurgency/Content/' + rest))
                game_files.add(pkg)
            elif root == mod:
                if EAL.does_asset_exist(remap(pkg)) and remap(pkg) not in stubs:
                    continue  # rebuilt by the tool (materials, textures, plain Blueprints): keep the rebuilt one
                ext = os.path.splitext(f)[1]
                new_rest = remap(pkg).split('/', 2)[2] + ext if renamed else rest
                entries.append((os.path.join(dp, f), '../../../Insurgency/Mods/%s/Content/%s' % (new_mod, new_rest)))
            else:
                skipped.add(pkg)
    for p in sorted(levels):
        log('graft pak: %s is the original level (its level Blueprint has logic); edits made to it in the editor '
            'are not in the game' % short(p))
    if game_files:
        log('graft pak: %d original file(s) under /Game/ included: %s' % (
            len(game_files), ', '.join(sorted(short(g) for g in game_files))))
    if skipped:
        log('graft pak: left out %d file(s) from other mods: %s' % (len(skipped), ', '.join(sorted(skipped))))
    if not entries:
        log('graft pak: nothing to pack from ' + src)
        return
    if renamed:
        entries = _graft_renamed_copies(entries, src, patched_dir, mod)
        if entries is None:
            return
    engine = unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())
    exe = os.path.join(engine, 'Binaries', 'Win64', 'UnrealPak.exe')
    if not os.path.exists(exe):
        log('UnrealPak.exe not found at ' + exe)
        return
    proj = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
    cooked = []
    for dp, _, fs in os.walk(proj):
        for f in fs:
            if f.lower() == ('%spakchunk0-windowsclient.pak' % new_mod).lower():
                cooked.append(os.path.join(dp, f))
    out_dir = os.path.dirname(max(cooked, key=os.path.getmtime)) if cooked else HERE
    out = os.path.join(out_dir, '%spakchunk1-WindowsClient_P.pak' % new_mod)
    resp = os.path.join(HERE, 'graft_pak.txt')
    with open(resp, 'w') as fh:
        for a, b in entries:
            fh.write('"%s" "%s"\n' % (a, b))
    r = subprocess.run([exe, out, '-create=' + resp], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        log('UnrealPak failed (%d): %s' % (r.returncode, (r.stdout + r.stderr)[-1500:]))
        return
    log('graft pak: %d files from %s -> %s' % (len(entries), src, out))
    if not cooked:
        todo('copy %s next to %spakchunk0-WindowsClient.pak (cook first) before uploading' % (out, new_mod))


def relink_scenarios(layout):
    levels = layout['levels']
    for p in used_persistents(layout, note=False):
        EL.load_level(remap(p))
        lighting = lighting_for(levels, p)
        mark_lighting_scenarios(lighting)
        scen = create_scenarios(layout, lighting, p)
        EL.load_level(remap(p))
        world_settings(layout, p, scen, lighting)


def _rest_of_main(layout, levels):
    built = built_levels(layout)
    for ls in layout.get('landscapes', []):
        if ls['level'] not in built:
            continue  # a skipped level's landscape (demo map, working copy)
        todo('import landscape into %s: file landscape/%s, Import panel Location %s, Scale %s, '
             'Section %dx%d quads, %d section(s)/component, %dx%d components, layers: %s' % (
                 short(ls['level']), ls['heightmap'], ls['import_panel_location'], ls['scale'],
                 ls['section_size_quads'], ls['section_size_quads'], ls['sections_per_component'],
                 ls['components'][0], ls['components'][1], ', '.join(ls['layers']) or 'none'))
    graftable = {remap(p) for p in graft_classes(layout)}
    for cls, acts in missing_classes.items():
        if cls.split('.')[0] in graftable:
            todo('%s (%d placed) is an original Blueprint with logic: run SMR > %s > Place original Blueprints to '
                 'place these, then Build graft pak after cooking.' % (short(cls), len(acts), menu_name()))
            continue
        todo('MISSING class %s (%d placed): recreate it at that path or place a replacement at: %s' % (
            cls, len(acts), '; '.join('%s @ %s yaw %.1f' % (a['name'], a['location'], a['rotation'].get('yaw', 0))
                                      for a in acts[:6])))
    finish()


def write_log():
    """rebuild_log.txt: this run's log and what is left by hand. Written at the end of every run, including one
    that stops with an error, so the file always belongs to the last run."""
    try:
        with open(os.path.join(HERE, 'rebuild_log.txt'), 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(log_lines))
            fh.write('\n\n==== STILL TO DO BY HAND ====\n' + '\n'.join('- ' + m for m in manual))
    except OSError as ex:
        unreal.log_warning('[SMR] rebuild_log.txt could not be written: %s' % ex)


def finish():
    summary = 'spawned %(spawned)d, failed %(failed)d, props skipped %(prop_fail)d' % stats
    log(summary)
    write_log()
    if FULL_RUN['on']:
        clear_progress()  # finished: the next run starts from scratch again
        try:
            write_todo()
        except Exception as ex:
            log('TODO.md could not be written: %s' % ex)
        unreal.EditorDialog.show_message('Map rebuild', '%s\n\nWhat is left is in TODO.html (opening it in your browser now).' % summary,
                                         unreal.AppMsgType.OK)
        _open(os.path.join(HERE, 'TODO.html') if os.path.exists(os.path.join(HERE, 'TODO.html'))
              else os.path.join(HERE, 'TODO.md'))
    else:
        unreal.EditorDialog.show_message('Map rebuild', '%s\n\n%d thing(s) left to do by hand.\nSee rebuild_log.txt' % (
            summary, len(manual)), unreal.AppMsgType.OK)


def _open(path):
    try:
        if sys.platform == 'win32':
            os.startfile(path)
    except Exception:
        pass


def _tex(path):
    return short(path) if isinstance(path, str) and path else '(none)'


def landscape_material_name(ls):
    orig = obj_path_of(ls.get('material')) or ''
    if orig.startswith('/Game/') and orig not in (LAYOUT_DATA.get('materials') or {}) and EAL.does_asset_exist(orig):
        return short(orig)  # stock game material the SDK has, used as is
    if _instance_on_game_master(orig):
        return short(orig)  # the mod's own instance of a stock game material, rebuilt as is
    return 'M_SMR_Landscape_' + short(ls['level'])  # the rebuilt layered material (a marketplace one is not in the SDK)


def write_todo():
    """TODO.md next to layout.json: everything left after the rebuild, in the order to do it, with exact values."""
    L = LAYOUT_DATA
    levels = L.get('levels') or {}
    persistents = used_persistents(L, note=False) if L.get('persistent_levels') else []
    main = short(remap(persistents[0])) if persistents else 'Map'
    out = ['# %s: what is left' % main, '',
           'The rebuild is done. Work down this list and tick things off. Exact values are given wherever the '
           'rebuild knows them; `rebuild_log.txt` has the full log.', '']

    def section(title):
        out.extend(['', '## ' + title, ''])

    n = [0]

    def step(title):
        n[0] += 1
        section('%d. %s' % (n[0], title))

    land_msgs = [m for m in manual if m.startswith(('LANDSCAPE', 'import landscape'))]
    ws_msgs = [m for m in manual if m.startswith('World Settings')]
    scen_msgs = [m for m in manual if m.startswith('scenario ')]
    other = [m for m in manual if m not in land_msgs + ws_msgs + scen_msgs]

    built = set(persistents)
    for p in persistents:
        built.update(x['level'] for x in levels.get(p, {}).get('streaming') or [])
    # landscape
    lss = [x for x in L.get('landscapes') or [] if x['level'] in built or not persistents]
    per_level = {}
    for x in lss:
        per_level[x['level']] = per_level.get(x['level'], 0) + 1
    seen_level = {}
    made_layers = set()  # layer info assets are shared: create each once, then reuse it
    for ls in lss:
        lvl = ls['level']
        seen_level[lvl] = seen_level.get(lvl, 0) + 1
        # not 'n': step() counts the sections in n[0], and rebinding it here broke every TODO for a map with a
        # landscape (the failure was swallowed by the caller, so no TODO.html was written at all)
        nth, total = seen_level[lvl], per_level[lvl]
        step('Import the landscape (%s)' % short(lvl) if total == 1 else
             'Import landscape %d of %d (%s)' % (nth, total, short(lvl)))
        if nth == 1:
            out += ['- [ ] Make `%s` the current level (Levels panel). If Landscapes from an earlier attempt are in '
                    'it, delete them first%s.' % (short(lvl), '' if total == 1 else
                                                  ', before the first of these %d imports only' % total), '']
        else:
            out += ['- [ ] Keep the landscapes already imported above; this one is added next to them.', '']
        out += ['Go to **Landscape mode > Manage > Import from File** and enter:', '',
                '| Setting | Value |', '|---|---|',
                '| Heightmap file | `landscape/%s` |' % ls['heightmap'],
                '| Material | `%s` |' % landscape_material_name(ls),
                '| Location (the panel uses the centre) | `%s` |' % ', '.join(str(x) for x in ls['import_panel_location']),
                '| Scale | `%s` |' % ', '.join(str(x) for x in ls['scale']),
                '| Section Size | %dx%d quads |' % (ls['section_size_quads'], ls['section_size_quads']),
                '| Sections Per Component | %dx%d |' % (ls['sections_per_component'], ls['sections_per_component']),
                '| Number of Components | %dx%d |' % (ls['components'][0], ls['components'][1]), '']
        if ls.get('layers'):
            items = ls['layers'].items() if isinstance(ls['layers'], dict) else [(x, None) for x in ls['layers']]
            out.append('- [ ] Set up each layer, then pick its file:')
            for layer, fn in items:
                f = 'landscape/%s' % (fn or '%s_Layer_%s.png' % (short(ls['level']), layer))
                if layer in made_layers:
                    out.append('  - [ ] %s: pick the existing `%s_LayerInfo` in its asset field (don\'t click **+**), '
                               'file `%s`' % (layer, layer, f))
                else:
                    made_layers.add(layer)
                    out.append('  - [ ] %s: click **+**, choose **Weight-Blended Layer (normal)** (creates `%s_LayerInfo`; '
                               'later landscapes reuse it), file `%s`' % (layer, layer, f))
        out += ['- [ ] Click **Import**. The Landscape actor\'s Location should then read `%s`.'
                % ', '.join(str(x) for x in ls['actor_location'])]
        r = ls.get('rotation') or {}
        rx, ry, rz = (round(float(r.get(k, 0))) % 360 for k in ('roll', 'pitch', 'yaw'))
        rx, ry, rz = ((v - 360) if v > 180 else v for v in (rx, ry, rz))
        if rx or ry or rz:
            # the import panel can't rotate; the actor pivots on its Location (the corner), so rotating it in
            # place after the import puts it exactly where the original was
            out.append('- [ ] The original was rotated: in the Landscape actor\'s Details set **Rotation** X `%d`, Y `%d`, '
                       'Z `%d` (leave Location as it is).' % (rx, ry, rz))
        out += ['', '- [ ] Now run **SMR > %s > Finish landscape (holes, layers, phys)**. The import panel has no '
                'field for any of it: it cuts the holes the original had, gives each paint layer its LayerInfo and '
                'sets the phys material. Then **Build > Build Paths**.' % menu_name(), '']
    # whatever the finish pass could not do
    if HOLES_LEFT or LAYERINFO_LEFT:
        step('Landscape bits to finish by hand')
    if LAYERINFO_LEFT:
        out += ['A paint layer with no **Layer Info** holds no weight data, so a layered terrain material comes out '
                'near-black however the layer PNGs look. In **Landscape mode > Paint**, for each Target Layer below, '
                'click the layer\'s asset field and pick the existing `<layer>_LayerInfo` (use **+ > Weight-Blended '
                'Layer** only if there is none):', '']
        for lvl, layer in LAYERINFO_LEFT:
            out.append('- [ ] `%s` in `%s`: assign `%s_LayerInfo`' % (layer, lvl, layer))
        out.append('')
    if HOLES_LEFT:
        out += ['The original cut holes in the terrain (stairwells, trenches, cellars, culverts). Left solid, the '
                'landscape fills them in and seals whatever was under them. The mask is in the landscape folder as '
                '`*_Layer_DataLayer.png` — white is where the terrain is cut away. Open it alongside the viewport '
                'and paint the holes with the **Visibility** tool, which is under **Landscape mode > Sculpt** (not '
                'Paint):', '']
        for lvl in dict.fromkeys(HOLES_LEFT):
            out.append('- [ ] `%s`: paint out every white patch in the mask' % lvl)
        out += ['- [ ] Then **Build > Build Paths** — visibility takes the collision away with the terrain, and '
                'navigation still thinks it is solid until paths are rebuilt.', '']
    # hand fixes
    if other:
        step('Fix by hand')
        out += ['- [ ] ' + m for m in other]
    if FOLIAGE_LEFT:
        step('Foliage that still needs painting')
        total = sum(x[2] for x in FOLIAGE_LEFT)
        out += ['There was no `%s` Blueprint, so foliage went in as single actors, capped per level to stay within '
                'memory (%d instances left out). Make an Actor Blueprint with that name and a Hierarchical Instanced '
                'Static Mesh component, then run **SMR > %s > Replace foliage only**:' % (HOLDER_BP, total, menu_name()), '',
                '| Level | Mesh | Not placed | Of |', '|---|---|---|---|']
        out += ['| %s | %s | %d | %d |' % tuple(x) for x in sorted(FOLIAGE_LEFT, key=lambda x: (x[0], -x[2]))]
        out.append('')
    if MISSING_ASSETS:
        step('Missing assets')
        out += ['Nothing could be found for these. Recreate them at the same path, or swap the actors that use them '
                'for something similar:', '']
        out += ['- [ ] `%s`' % m for m in MISSING_ASSETS]
    # scenarios / world settings
    step('Scenarios and World Settings')
    for pkg in (L.get('scenarios') or {}):
        out.append('- [ ] Open `%s`: no Objective or Spawn Zone entry should be red. If any are, run '
                   '**SMR > %s > Re-link scenarios only** (or pick them with the eyedropper).' %
                   (renamed_short(short(pkg)), menu_name()))  # the scenario asset as the rebuilt mod names it
    out += ['- [ ] ' + m for m in scen_msgs + ws_msgs]
    if not ws_msgs:
        out.append('- [ ] World Settings were set by the rebuild: check Default Scenarios, Lighting Scenarios and '
                   'Tactical Map Material are filled in.')
    # exposure
    hot = []
    built = built_levels(L)
    for lv, d in levels.items():
        if lv not in built:
            continue  # not rebuilt (an author's working map): nothing in the project to check
        for a in d.get('actors', []):
            st = (a.get('props') or {}).get('Settings') or {}
            if a['class'].endswith('PostProcessVolume') and any(
                    k.startswith('bOverride_AutoExposure') and v for k, v in st.items()):
                hot.append((short(lv), a['name']))
    if hot:
        step('Check the exposure')
        out += ['These post-process volumes override auto-exposure with values from the old engine, which can make '
                'the map look washed out in game:', '']
        for lv, a in hot:
            out.append('- [ ] `%s` in `%s`: make that level visible (Levels panel) and look at the viewport with '
                       'Realtime on. If it is washed out, untick its Exposure overrides.' % (a, lv))
    lighting_keys = []
    for p in persistents:
        lighting_keys += [k for k in lighting_for(levels, p) if k not in lighting_keys]
    # moddata
    md = next(iter((L.get('moddata') or {}).values()), {}) or {}
    step('ModData')
    out += ['Open the **ModData** asset in your mod\'s root folder:', '']
    out.append('- [ ] In Game Name: `%s`' % (md.get('InGameName') or main))
    if RENAMES:
        out.append('- [ ] Map entries (Loading Screens, Map Thumbnails and any map list) use the new map name `%s`' % main)
    for field, label in (('LoadingScreens', 'Loading Screens'), ('MapThumbnails', 'Map Thumbnails')):
        entries = md.get(field) or {}
        e = entries.get(main) or entries.get(short(persistents[0]) if persistents else '') or next((v for v in entries.values() if (v or {}).get('LightingScenarioTextures')),
                                      None) or next(iter(entries.values()), None)
        if not e:
            continue
        lst = e.get('LightingScenarioTextures') or {}
        first = next(iter(lighting_keys), None)
        line = '- [ ] %s: one element, key `%s`, Default Texture `%s`' % (
            label, main, _tex(lst.get(first) or e.get('DefaultTexture')))
        if lst:
            line += ', Lighting Scenario Textures ' + ', '.join('`%s` -> `%s`' % (k, _tex(v)) for k, v in lst.items())
        out.append(line)
    # build and test
    step('Build and test')
    if L.get('grass_types'):
        out.append('- [ ] **Build > Build Grass Maps**')
    scen_levels = []
    for p in persistents:
        scen_levels += [(k, short(v)) for k, v in lighting_for(levels, p).items() if (k, short(v)) not in scen_levels]
    if len(scen_levels) > 1:
        flag_left = [lv for _, lv in scen_levels if lv in SCENARIO_FLAG_LEFT]
        if flag_left:
            out.append('- [ ] In the **Levels** window, right-click %s and make each a **Lighting Scenario** (the '
                       'level then shows the lighting scenario icon). Without it every scenario shows whichever one '
                       'was built last.' % ', '.join('`%s`' % lv for lv in flag_left))
        out.append('- [ ] **Build > Lighting Quality > Medium** (Production looks almost the same and takes far longer).')
        for k, lv in scen_levels:
            others = ', '.join('`%s`' % o for _, o in scen_levels if o != lv)
            out.append('- [ ] %s lighting: in the **Levels** window hide %s and show `%s`, then **Build > Build '
                       'Lighting Only**. When asked about hidden levels, answer **Yes** (not *Yes All*). Save All.'
                       % (k, others, lv))
    else:
        out.append('- [ ] **Build > Lighting Quality > Medium** (Production looks almost the same and takes far '
                   'longer), then **Build Lighting**. Save All.')
    out += ['- [ ] Cook and play it on a server: every mode loads, objectives capture, lighting looks right.', '']
    keys = lighting_keys or ['Day']
    out += ['Travel commands:', '', '```']
    for pkg in (L.get('scenarios') or {}):
        for k in keys:
            out.append('travel %s?Scenario=%s?Lighting=%s' % (main, short(remap(pkg)), k))
    out += ['```', '', 'MapCycle.txt:', '', '```']
    for pkg in (L.get('scenarios') or {}):
        for k in keys:
            out.append('(Scenario="%s",Lighting="%s")' % (short(remap(pkg)), k))
    out.append('```')
    # upload
    step('Upload')
    out += ['- [ ] Credit the original author in the summary and description. If they are still around, ask first.',
            ('- [ ] Tell server owners in the description that the map name changed to `%s` (the travel commands above), '
             'so they update their map cycles.' % main) if RENAMES else
            ('- [ ] Add to the description: *Unsubscribe from the original mod before using this one*. This rebuild '
             'uses the original\'s mod name, so with both installed the game loads the original.'),
            '- [ ] Summary is limited to 250 characters; the mod.io logo is 16:9 (e.g. 1280x720 PNG).']
    with open(os.path.join(HERE, 'TODO.md'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    with open(os.path.join(HERE, 'TODO.html'), 'w', encoding='utf-8') as fh:
        fh.write(todo_html(out, main))


TODO_CSS = '''
:root { --bg:#fbfaf7; --fg:#1d1d1f; --mut:#6b6b70; --line:#e2dfd8; --card:#fff; --acc:#c96a2b; --code:#f2efe8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1b1a19; --fg:#ecebe8; --mut:#a3a19c; --line:#34322f; --card:#232220; --acc:#e08a4d; --code:#2c2a27; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 "Segoe UI", system-ui, sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 28px 18px 80px; }
h1 { font-size: 26px; margin: 0 0 6px; }
.bar { position: sticky; top: 0; background: var(--bg); padding: 10px 0; border-bottom: 1px solid var(--line);
       z-index: 2; display: flex; align-items: center; gap: 12px; }
.bar .track { flex: 1; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; }
.bar .fill { height: 100%; width: 0; background: var(--acc); transition: width .2s; }
.bar .num { font-variant-numeric: tabular-nums; color: var(--mut); min-width: 90px; text-align: right; }
.bar button { background: none; border: 1px solid var(--line); color: var(--mut); border-radius: 6px; padding: 3px 9px;
              cursor: pointer; font: inherit; font-size: 13px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 4px 18px 14px;
          margin: 16px 0; }
section.done { opacity: .55; }
h2 { font-size: 18px; margin: 14px 0 8px; display: flex; gap: 10px; align-items: baseline; }
h2 .count { font-size: 13px; color: var(--mut); font-weight: normal; margin-left: auto; }
p { margin: 8px 0; }
ul { list-style: none; padding-left: 0; margin: 6px 0; }
ul ul { padding-left: 26px; }
li { margin: 4px 0; }
label { display: flex; gap: 9px; align-items: flex-start; cursor: pointer; }
label input { margin-top: 4px; width: 16px; height: 16px; accent-color: var(--acc); flex: none; }
input:checked + span { text-decoration: line-through; color: var(--mut); }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; font: 13px Consolas, monospace;
       overflow-wrap: anywhere; }
pre { background: var(--code); padding: 10px 12px; border-radius: 8px; overflow-x: auto; font: 13px/1.5 Consolas, monospace; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 8px 0; width: 100%; }
td, th { border: 1px solid var(--line); padding: 5px 9px; text-align: left; vertical-align: top; }
th { color: var(--mut); font-weight: 600; }
td:last-child code { user-select: all; }
'''

TODO_JS = '''
const key = 'smr-todo:' + document.title;
let saved = {};
try { saved = JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) {}
const boxes = [...document.querySelectorAll('input[type=checkbox]')];
// a tick belongs to the item's text (plus which copy of it), not its position: a re-run that adds or drops items
// would otherwise move old ticks onto other steps
const seen = {};
const ids = boxes.map(b => {
  const t = b.parentElement.textContent.trim();
  seen[t] = (seen[t] || 0) + 1;
  return t + '#' + seen[t];
});
function update() {
  const n = boxes.filter(b => b.checked).length;
  document.querySelector('.fill').style.width = (boxes.length ? 100 * n / boxes.length : 0) + '%';
  document.querySelector('.num').textContent = n + ' / ' + boxes.length;
  document.querySelectorAll('section').forEach(sec => {
    const bs = [...sec.querySelectorAll('input[type=checkbox]')];
    const c = sec.querySelector('h2 .count');
    if (c) c.textContent = bs.length ? bs.filter(b => b.checked).length + ' / ' + bs.length : '';
    sec.classList.toggle('done', bs.length > 0 && bs.every(b => b.checked));
  });
}
boxes.forEach((b, i) => {
  b.checked = !!saved[ids[i]];
  b.addEventListener('change', () => {
    if (b.checked) saved[ids[i]] = true; else delete saved[ids[i]];
    try { localStorage.setItem(key, JSON.stringify(saved)); } catch (e) {}
    update();
  });
});
document.querySelector('.reset').addEventListener('click', () => {
  if (!confirm('Untick everything?')) return;
  saved = {}; boxes.forEach(b => b.checked = false);
  try { localStorage.removeItem(key); } catch (e) {}
  update();
});
update();
'''


def _inline(t):
    """`code`, **bold**, *italic* on already-escaped text."""
    import html as H
    import re as R
    parts = R.split(r'(`[^`]*`)', t)
    out = []
    for p in parts:
        if p.startswith('`') and p.endswith('`') and len(p) > 1:
            out.append('<code>%s</code>' % H.escape(p[1:-1]))
        else:
            p = H.escape(p)
            p = R.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', p)
            p = R.sub(r'(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])', r'<i>\1</i>', p)
            out.append(p)
    return ''.join(out)


def todo_html(lines, title):
    """The TODO as a page: headings become cards, '- [ ]' items real checkboxes (remembered in the browser),
    tables and code blocks rendered. Handles exactly the markdown write_todo produces."""
    import html as H
    body, i, n = [], 0, len(lines)
    open_sec = False
    depth = 0  # open <ul> levels

    def close_lists(to=0):
        nonlocal depth
        while depth > to:
            body.append('</li></ul>')
            depth -= 1

    while i < n:
        ln = lines[i]
        st = ln.strip()
        if ln.startswith('# '):
            body.append('<h1>%s</h1>' % _inline(ln[2:]))
        elif ln.startswith('## '):
            close_lists()
            if open_sec:
                body.append('</section>')
            body.append('<section><h2>%s<span class="count"></span></h2>' % _inline(ln[3:]))
            open_sec = True
        elif st.startswith('```'):
            close_lists()
            code = []
            i += 1
            while i < n and not lines[i].strip().startswith('```'):
                code.append(lines[i])
                i += 1
            body.append('<pre><code>%s</code></pre>' % H.escape('\n'.join(code)))
        elif st.startswith('|'):
            close_lists()
            rows = []
            while i < n and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(set(c) <= set('-: ') for c in cells):
                    rows.append(cells)
                i += 1
            i -= 1
            if rows:
                h = ''.join('<th>%s</th>' % _inline(c) for c in rows[0])
                r = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % _inline(c) for c in row) for row in rows[1:])
                body.append('<table><tr>%s</tr>%s</table>' % (h, r))
        elif st.startswith('- '):
            lvl = (len(ln) - len(ln.lstrip(' '))) // 2 + 1
            text = st[2:]
            box = text.startswith('[ ] ') or text.startswith('[x] ')
            if box:
                text = text[4:]
            if lvl > depth:
                while depth < lvl:
                    body.append('<ul><li>' if depth == lvl - 1 else '<ul><li>')
                    depth += 1
            else:
                close_lists(lvl)
                body.append('</li><li>')
            if box:
                body.append('<label><input type="checkbox"><span>%s</span></label>' % _inline(text))
            else:
                body.append(_inline(text))
        elif st:
            close_lists()
            body.append('<p>%s</p>' % _inline(st))
        i += 1
    close_lists()
    if open_sec:
        body.append('</section>')
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>%s TODO</title><style>%s</style></head><body><main>'
            '<div class="bar"><div class="track"><div class="fill"></div></div><span class="num"></span>'
            '<button class="reset" type="button">Reset</button></div>%s</main><script>%s</script></body></html>')
    return page % (H.escape(title), TODO_CSS, '\n'.join(body), TODO_JS)


main()
