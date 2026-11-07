"""Minimal UE4 (4.2x, cooked, tagged-property) package parser: names, imports, exports, properties."""
import struct, os


class UAssetError(Exception):
    pass

class R:
    def __init__(s, b, p=0): s.b, s.p = b, p
    def u(s, f):
        v = struct.unpack_from(f, s.b, s.p); s.p += struct.calcsize(f); return v
    def i32(s): return s.u('<i')[0]
    def u32(s): return s.u('<I')[0]
    def i64(s): return s.u('<q')[0]
    def u16(s): return s.u('<H')[0]
    def u8(s): return s.u('<B')[0]
    def f32(s): return s.u('<f')[0]
    def guid(s): g = s.b[s.p:s.p+16]; s.p += 16; return g.hex()
    def fstr(s):
        n = s.i32()
        if n == 0: return ''
        if n < 0:
            n = -n; v = s.b[s.p:s.p+n*2].decode('utf-16-le'); s.p += n*2
        else:
            v = s.b[s.p:s.p+n].decode('latin-1'); s.p += n
        return v.rstrip('\x00')

class Package:
    @classmethod
    def from_file(cls, path):
        base = os.path.splitext(path)[0]
        hdr = open(path, 'rb').read()
        ex = base + '.uexp'
        return cls(hdr + (open(ex, 'rb').read() if os.path.exists(ex) else b''), name=os.path.basename(base))

    def __init__(self, data, name=''):
        self.name = name
        self.data = data
        r = R(self.data)
        if r.u32() != 0x9E2A83C1:
            raise UAssetError('not a uasset/umap')
        legacy = r.i32()
        if legacy != -4: r.i32()
        self.ue4ver = r.i32() or 522; r.i32()
        cv = r.i32()
        for _ in range(cv): r.guid(); r.i32()
        self.total_header = r.i32()
        r.fstr()
        self.pkgflags = r.u32()
        if self.pkgflags & 0x2000:
            raise UAssetError('unversioned property serialization (cooked with -unversioned + UE5-style); not supported')
        name_count, name_off = r.i32(), r.i32()
        if self.ue4ver >= 516 and not (self.pkgflags & 0x80000000): r.fstr()
        if self.ue4ver >= 459: r.i32(); r.i32()
        export_count, export_off = r.i32(), r.i32()
        import_count, import_off = r.i32(), r.i32()
        r.i32()  # depends
        if self.ue4ver >= 384: r.i32(); r.i32()
        if self.ue4ver >= 510: r.i32()
        r.i32()  # thumbnail
        r.guid()
        gens = r.i32()
        for _ in range(gens): r.i32(); r.i32()
        def ev():
            a = r.u('<HHHI'); b = r.fstr(); return f"{a[0]}.{a[1]}.{a[2]}-{a[3]}+{b}"
        self.saved_by = ev(); self.compat = ev()
        self.bulk_start = 0
        try:
            r.u32()  # compression flags
            if r.i32() == 0:  # compressed chunks
                r.u32()  # package source
                for _ in range(r.i32()): r.fstr()
                if legacy > -7: r.i32()  # texture allocations
                r.i32()  # asset registry offset
                self.bulk_start = r.i64()
        except Exception:
            self.bulk_start = 0

        r.p = name_off
        self.names = []
        for _ in range(name_count):
            self.names.append(r.fstr())
            if self.ue4ver >= 504: r.u16(); r.u16()
        self.import_off = import_off
        r.p = import_off
        self.imports = []
        for _ in range(import_count):
            cp = self.fname(r); cn = self.fname(r); outer = r.i32(); on = self.fname(r)
            self.imports.append(dict(class_package=cp, class_name=cn, outer=outer, name=on))
        r.p = export_off
        self.exports = []
        for _ in range(export_count):
            e = dict(cls=r.i32(), sup=r.i32())
            if self.ue4ver >= 508: e['tmpl'] = r.i32()
            e['outer'] = r.i32(); e['name'] = self.fname(r); e['flags'] = r.u32()
            e['size'] = r.i64(); e['off'] = r.i64()
            r.u('<iii'); r.guid(); r.u32()
            if self.ue4ver >= 365: r.i32()
            if self.ue4ver >= 485: r.i32()
            if self.ue4ver >= 507: r.u('<iiiii')
            self.exports.append(e)

    def fname(self, r):
        i, n = r.i32(), r.i32()
        s = self.names[i]
        return s if n == 0 else f"{s}_{n-1}"

    # ---- object references ----
    def ref(self, idx):
        if idx == 0: return None
        if idx < 0: return ('import', -idx - 1)
        return ('export', idx - 1)

    def full_name(self, idx):
        if idx == 0: return None
        parts = []
        while idx != 0:
            if idx < 0:
                im = self.imports[-idx - 1]; parts.append(im['name']); idx = im['outer']
            else:
                ex = self.exports[idx - 1]; parts.append(ex['name']); idx = ex['outer']
        parts.reverse()
        # package path first, then object path
        return parts[0] + ('.' + ':'.join(parts[1:]) if len(parts) > 1 else '') if parts[0].startswith('/') else '.'.join(parts)

    def class_of(self, e):
        return self.full_name(e['cls'])

    # ---- properties ----
    def read_props(self, r, end):
        props = {}
        while r.p < end:
            name = self.fname(r)
            if name == 'None': break
            typ = self.fname(r); size = r.i32(); aidx = r.i32()
            extra = {}
            if typ == 'StructProperty':
                extra['struct'] = self.fname(r); r.guid()
            elif typ == 'BoolProperty':
                extra['bool'] = r.u8()
            elif typ in ('ByteProperty', 'EnumProperty'):
                extra['enum'] = self.fname(r)
            elif typ in ('ArrayProperty', 'SetProperty'):
                extra['inner'] = self.fname(r)
            elif typ == 'MapProperty':
                extra['inner'] = self.fname(r); extra['value'] = self.fname(r)
            if self.ue4ver >= 503 and r.u8(): r.guid()
            start = r.p
            try:
                val = self.read_value(r, typ, size, extra)
            except Exception as ex:
                val = {'_unparsed': typ, 'err': str(ex)}
            r.p = start + size
            key = name if aidx == 0 else f"{name}[{aidx}]"
            props[key] = val
        return props

    def read_value(self, r, typ, size, extra):
        if typ == 'BoolProperty': return bool(extra['bool'])
        if typ == 'IntProperty': return r.i32()
        if typ == 'UInt32Property': return r.u32()
        if typ == 'Int64Property': return r.i64()
        if typ == 'FloatProperty': return r.f32()
        if typ in ('ObjectProperty', 'ClassProperty', 'WeakObjectProperty', 'LazyObjectProperty'):
            return self.full_name(r.i32())
        if typ == 'SoftObjectProperty':
            p = self.fname(r); sub = r.fstr(); return p + (':' + sub if sub else '')
        if typ in ('NameProperty',): return self.fname(r)
        if typ == 'StrProperty': return r.fstr()
        if typ == 'TextProperty': return self.read_text(r)
        if typ == 'ByteProperty':
            return self.fname(r) if extra['enum'] != 'None' and size == 8 else r.u8()
        if typ == 'EnumProperty': return self.fname(r)
        if typ == 'StructProperty': return self.read_struct(r, extra['struct'], r.p + size)
        if typ == 'ArrayProperty': return self.read_array(r, extra['inner'], r.p + size)
        if typ == 'SetProperty':
            # TSet: elements removed from the archetype's set, then the elements (no inner tag)
            for _ in range(r.i32()):
                self._simple(r, extra['inner'])
            return [self._simple(r, extra['inner']) for _ in range(r.i32())]
        if typ == 'MapProperty': return self.read_map(r, extra['inner'], extra['value'], r.p + size)
        return {'_raw': typ, 'size': size}

    def _simple(self, r, t):
        if t in ('NameProperty', 'EnumProperty'): return self.fname(r)
        if t == 'StrProperty': return r.fstr()
        if t in ('ObjectProperty', 'ClassProperty'): return self.full_name(r.i32())
        if t == 'SoftObjectProperty':
            pth = self.fname(r); sub = r.fstr(); return pth + (':' + sub if sub else '')
        if t == 'IntProperty': return r.i32()
        if t == 'FloatProperty': return r.f32()
        if t == 'BoolProperty': return bool(r.u8())
        if t == 'ByteProperty': return r.u8()
        raise ValueError('map element type ' + t)

    def read_map(self, r, kt, vt, end):
        removed = r.i32()
        for _ in range(removed):
            self._simple(r, kt)
        n = r.i32()
        out = {}
        for _ in range(n):
            k = self._simple(r, kt)
            v = self.read_props(r, end) if vt == 'StructProperty' else self._simple(r, vt)
            out[str(k)] = v
        return out

    def read_text(self, r):
        r.u32()  # flags
        ht = struct.unpack('<b', r.b[r.p:r.p + 1])[0]; r.p += 1
        if ht == -1:  # None history
            if r.i32():
                return r.fstr()
            return ''
        if ht == 0:  # Base: namespace, key, source string
            r.fstr(); r.fstr()
            return r.fstr()
        if ht == 11:  # StringTableEntry
            return {'string_table': self.fname(r), 'key': r.fstr()}
        return {'_raw': 'TextProperty'}

    def read_struct(self, r, sn, end):
        if sn in ('Vector',): return list(r.u('<fff'))
        if sn == 'Rotator': return dict(zip(('pitch', 'yaw', 'roll'), r.u('<fff')))
        if sn in ('Quat', 'Vector4', 'Plane'): return list(r.u('<ffff'))
        if sn in ('PerPlatformFloat', 'PerPlatformInt', 'PerPlatformBool'):
            # native: bool bCooked (4 bytes), then the default value; editor data adds a per-platform map after it
            r.u32()
            v = r.f32() if sn == 'PerPlatformFloat' else r.i32()
            r.p = end
            return bool(v) if sn == 'PerPlatformBool' else v
        if sn == 'IntVector': return list(r.u('<iii'))
        if sn == 'Box2D': return {'min': list(r.u('<ff')), 'max': list(r.u('<ff')), 'valid': r.u8()}
        if sn in ('LinearColor',): return list(r.u('<ffff'))
        if sn == 'Color': b, g, rr, a = r.u('<BBBB'); return [rr, g, b, a]
        if sn == 'Vector2D': return list(r.u('<ff'))
        if sn == 'IntPoint': return list(r.u('<ii'))
        if sn == 'Guid': return r.guid()
        if sn == 'Box': return {'min': list(r.u('<fff')), 'max': list(r.u('<fff')), 'valid': r.u8()}
        if sn in ('SoftObjectPath', 'SoftClassPath'):
            p = self.fname(r); sub = r.fstr(); return p + (':' + sub if sub else '')
        if sn == 'GameplayTagContainer':
            n = r.i32(); return [self.fname(r) for _ in range(n)]
        if sn == 'GameplayTag': return self.fname(r)
        return self.read_props(r, end)

    def read_array(self, r, inner, end):
        n = r.i32()
        if inner == 'StructProperty':
            # inner tag
            self.fname(r); self.fname(r); r.i32(); r.i32(); sn = self.fname(r); r.guid()
            if self.ue4ver >= 503 and r.u8(): r.guid()
            return [self.read_struct(r, sn, end) for _ in range(n)]
        out = []
        for _ in range(n):
            if inner in ('ObjectProperty', 'ClassProperty'): out.append(self.full_name(r.i32()))
            elif inner == 'IntProperty': out.append(r.i32())
            elif inner == 'FloatProperty': out.append(r.f32())
            elif inner == 'NameProperty' or inner == 'EnumProperty': out.append(self.fname(r))
            elif inner == 'StrProperty': out.append(r.fstr())
            elif inner == 'BoolProperty': out.append(bool(r.u8()))
            elif inner == 'ByteProperty': out.append(r.u8())
            elif inner == 'SoftObjectProperty':
                p = self.fname(r); sub = r.fstr(); out.append(p + (':' + sub if sub else ''))
            else:
                return {'_rawarray': inner, 'count': n}
        return out

    def export_props(self, i):
        e = self.exports[i]
        r = R(self.data, e['off'])
        return self.read_props(r, e['off'] + e['size'])


def texture_mips(pkg, idx):
    """Parse cooked FTexturePlatformData of a Texture2D export.
    Returns (pixel_format, [ {w,h,flags,size,offset,data|None} ... ])."""
    e = pkg.exports[idx]
    d = pkg.data[e['off']:e['off'] + e['size']]
    # locate the pixel format string; header fields precede it
    for pf in (b'PF_B8G8R8A8', b'PF_DXT1', b'PF_DXT5', b'PF_BC5', b'PF_G8', b'PF_BC4', b'PF_BC7',
               b'PF_FloatRGBA', b'PF_G16', b'PF_R8G8B8A8'):
        k = d.find(struct.pack('<i', len(pf) + 1) + pf + b'\0')
        if k >= 12:
            break
    else:
        return None, []
    r = R(d, k)
    fmt = r.fstr()
    r.i32()  # first mip to load
    nmips = r.i32()
    mips = []
    for _ in range(nmips):
        r.i32()  # bCooked
        flags = r.u32()
        if flags & 0x2000:  # BULKDATA_Size64Bit
            r.i64(); size = r.i64()  # element count, then size on disk
        else:
            r.i32(); size = r.i32()
        off = r.i64()
        if flags & 0x4000:  # BULKDATA_DuplicateNonOptionalPayload: a second copy's flags/size/offset follow
            dflags = r.u32()
            if dflags & 0x2000: r.i64()
            else: r.i32()
            r.i64()
        data = None
        inline = not (flags & 0x100) and not (flags & 0x1)  # not separate file, not end-of-file
        if flags & 0x40 or inline:
            data = d[r.p:r.p + size]
            r.p += size
        w, h = r.i32(), r.i32()
        r.i32()  # depth
        mips.append({'w': w, 'h': h, 'flags': flags, 'size': size, 'offset': off, 'data': data})
    return fmt, mips
