"""Minimal UE4 .pak reader (versions 8-11, unencrypted). Supports uncompressed and zlib entries."""
import struct
import zlib

PAK_MAGIC = 0x5A6F12E1


class PakError(Exception):
    pass


class _R:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def u(self, f):
        v = struct.unpack_from(f, self.b, self.p)
        self.p += struct.calcsize(f)
        return v if len(v) > 1 else v[0]

    def fstr(self):
        n = self.u('<i')
        if n == 0:
            return ''
        if n < 0:
            s = self.b[self.p:self.p - n * 2].decode('utf-16-le')
            self.p += -n * 2
        else:
            s = self.b[self.p:self.p + n].decode('utf-8', 'replace')
            self.p += n
        return s.rstrip('\x00')


class PakEntry:
    __slots__ = ('path', 'offset', 'size', 'usize', 'comp', 'blocks', 'block_size', 'encrypted')

    def __repr__(self):
        return f'<PakEntry {self.path} {self.usize}b comp={self.comp}>'


class Pak:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        self.f.seek(0, 2)
        size = self.f.tell()
        footer = None
        # footer layouts: v8A (UE4.22, 4 methods) 189b, v8B/v10/v11 221b, v9 (+frozen-index byte) 222b
        for fsize, nmethods, versions in ((221, 5, (8, 10, 11)), (222, 5, (9,)), (189, 4, (8,))):
            if size < fsize:
                continue
            self.f.seek(size - fsize)
            b = self.f.read(fsize)
            if struct.unpack_from('<I', b, 17)[0] == PAK_MAGIC and struct.unpack_from('<I', b, 21)[0] in versions:
                footer, self.nmethods = b, nmethods
                self.method_off = 62 if fsize == 222 else 61
                break
        if footer is None:
            raise PakError('not a UE4 pak (v8-v11 expected)')
        self.encrypted_index = footer[16]
        self.version, self.index_offset, self.index_size = struct.unpack_from('<Iqq', footer, 21)
        mo = self.method_off
        self.methods = [''] + [footer[mo + i * 32:mo + (i + 1) * 32].rstrip(b'\0').decode()
                               for i in range(self.nmethods)]
        # UE4.22 (v8A) stored the compression method index as a single byte
        self.comp_u8 = self.version == 8 and self.nmethods == 4
        if self.encrypted_index:
            raise PakError('pak index is encrypted (AES key needed)')
        if self.version < 8:
            raise PakError('pak version %d not supported' % self.version)
        self.f.seek(self.index_offset)
        idx = self.f.read(self.index_size)
        self.entries = {}
        if self.version >= 10:
            self._read_index_v10(idx)
        else:
            self._read_index_legacy(idx)

    # ---------------------------------------------------------------- index
    def _read_entry(self, r):
        e = PakEntry()
        e.offset = r.u('<q')
        e.size = r.u('<q')
        e.usize = r.u('<q')
        e.comp = r.u('<B') if self.comp_u8 else r.u('<I')
        r.p += 20  # sha1
        e.blocks = []
        if e.comp:
            n = r.u('<I')
            e.blocks = [r.u('<qq') for _ in range(n)]
        e.encrypted = bool(r.u('<B'))
        e.block_size = r.u('<I')
        return e

    def _read_index_legacy(self, idx):
        r = _R(idx)
        self.mount = r.fstr()
        n = r.u('<i')
        for _ in range(n):
            name = r.fstr()
            e = self._read_entry(r)
            e.path = name
            self.entries[name] = e

    def _decode_entry(self, b, p):
        bits = struct.unpack_from('<I', b, p)[0]
        p += 4
        e = PakEntry()
        if bits & 0x3f == 0x3f:
            e.block_size = struct.unpack_from('<I', b, p)[0]
            p += 4
        else:
            e.block_size = (bits & 0x3f) << 11
        e.comp = (bits >> 23) & 0x3f
        e.encrypted = bool(bits & (1 << 22))
        nblocks = (bits >> 6) & 0xffff
        if bits & (1 << 31):
            e.offset = struct.unpack_from('<I', b, p)[0]; p += 4
        else:
            e.offset = struct.unpack_from('<q', b, p)[0]; p += 8
        if bits & (1 << 30):
            e.usize = struct.unpack_from('<I', b, p)[0]; p += 4
        else:
            e.usize = struct.unpack_from('<q', b, p)[0]; p += 8
        if e.comp:
            if bits & (1 << 29):
                e.size = struct.unpack_from('<I', b, p)[0]; p += 4
            else:
                e.size = struct.unpack_from('<q', b, p)[0]; p += 8
        else:
            e.size = e.usize
        # header that precedes the data in the file
        hdr = 8 + 8 + 8 + 4 + 20 + 1 + 4 + ((4 + nblocks * 16) if e.comp else 0)
        e.blocks = []
        if e.comp:
            if nblocks == 1 and not e.encrypted:
                e.blocks = [(hdr, hdr + e.size)]
            else:
                start = hdr
                for _ in range(nblocks):
                    bs = struct.unpack_from('<I', b, p)[0]; p += 4
                    e.blocks.append((start, start + bs))
                    start += (bs + 15) & ~15 if e.encrypted else bs
        return e

    def _read_index_v10(self, idx):
        r = _R(idx)
        self.mount = r.fstr()
        r.u('<i')  # entry count
        r.u('<Q')  # path hash seed
        if r.u('<I'):
            r.p += 8 + 8 + 20
        has_full = r.u('<I')
        full_off = full_size = None
        if has_full:
            full_off, full_size = r.u('<qq')
            r.p += 20
        enc_size = r.u('<i')
        encoded = idx[r.p:r.p + enc_size]
        r.p += enc_size
        nfiles = r.u('<i')
        loose = [self._read_entry(r) for _ in range(nfiles)]
        if not has_full:
            raise PakError('pak has no full directory index; cannot list files')
        self.f.seek(full_off)
        d = _R(self.f.read(full_size))
        for _ in range(d.u('<i')):
            dname = d.fstr()
            for _ in range(d.u('<i')):
                fname = d.fstr()
                loc = d.u('<i')
                if loc < 0:
                    e = loose[-loc - 1]
                else:
                    e = self._decode_entry(encoded, loc)
                path = (dname + fname).lstrip('/')
                e.path = path
                self.entries[path] = e

    # ---------------------------------------------------------------- data
    def read(self, path):
        e = self.entries[path]
        if e.encrypted:
            raise PakError('%s is encrypted' % path)
        # every entry starts with a serialized copy of its header; skip it
        self.f.seek(e.offset)
        hdr = self.f.read(64 + len(e.blocks) * 16)
        r = _R(hdr)
        inline = self._read_entry(r)
        data_start = e.offset + r.p
        if not e.comp:
            self.f.seek(data_start)
            return self.f.read(e.usize)
        method = self.methods[e.comp] if e.comp < len(self.methods) else '?'
        if method.lower() != 'zlib':
            raise PakError('%s uses %s compression (only zlib supported)' % (path, method))
        out = bytearray()
        for (s, t) in inline.blocks:
            # block offsets are relative to the entry start in v5+
            self.f.seek(e.offset + s)
            out += zlib.decompress(self.f.read(t - s))
        return bytes(out)

    def close(self):
        """Release the file. Windows keeps an open pak locked, so mod.io couldn't update the mod while the app runs."""
        try:
            self.f.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def extract_all(self, dest):
        import os
        for p in self.entries:
            out = os.path.join(dest, p)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as fh:
                fh.write(self.read(p))
        return len(self.entries)
