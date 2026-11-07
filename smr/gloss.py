"""Read how glossy a cooked custom material is, straight from its compiled pixel shaders.

A cooked UMaterial has no node graph left, only shader bytecode. UE writes the material's Roughness through
View.RoughnessOverrideParameter (Roughness * P.y + P.x), so the one instruction that reads both halves of that
parameter tells us the roughness: a literal there is the material's constant roughness, a plain copy of P.x means
roughness 0, and a register means it comes from a texture or parameter.
"""
import struct

# ----------------------------------------------------------------------------- LZ4 (block format), pure Python


class _Bad(Exception):
    pass


def lz4_block(src, size):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                if i >= n:
                    raise _Bad()
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if i + lit > n:
            raise _Bad()
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        if i + 2 > n:
            raise _Bad()
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0 or off > len(out):
            raise _Bad()
        ml = tok & 15
        if ml == 15:
            while True:
                if i >= n:
                    raise _Bad()
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        if len(out) + ml > size:
            raise _Bad()
        st = len(out) - off
        if off >= ml:
            out += out[st:st + ml]
        else:
            for k in range(ml):
                out.append(out[st + k])
    if len(out) != size:
        raise _Bad()
    return bytes(out)


def shader_blobs(d):
    """DXBC pixel shaders inside a material package's FShaderEntry blocks
    ({TArray<uint8> Code (LZ4), int32 UncompressedSize, uint8 Frequency})."""
    out = []
    q, n = 0, len(d)
    while q < n - 9:
        ln = struct.unpack_from('<I', d, q)[0]
        if 16 < ln < 2_000_000 and q + 4 + ln + 5 <= n:
            un, fr = struct.unpack_from('<IB', d, q + 4 + ln)
            if ln <= un < 8_000_000 and fr < 16 and (d[q + 4] >> 4) > 0:
                try:
                    code = lz4_block(d[q + 4:q + 4 + ln], un)
                except _Bad:
                    code = None
                if code:
                    j = code.find(b'DXBC')
                    if j >= 0 and j + 32 <= len(code):
                        size = struct.unpack_from('<I', code, j + 24)[0]
                        blob = code[j:j + size]
                        if _shader_type(blob) == 'PS':
                            out.append(blob)
                    q += 4 + ln + 5
                    continue
        q += 1
    return out


# ----------------------------------------------------------------------------- SM4/5 bytecode

def _chunk(blob, tags=(b'SHEX', b'SHDR')):
    if len(blob) < 32 or blob[:4] != b'DXBC':
        return None
    cnt = struct.unpack_from('<I', blob, 28)[0]
    for k in range(min(cnt, 64)):
        o = struct.unpack_from('<I', blob, 32 + 4 * k)[0]
        if blob[o:o + 4] in tags:
            ln = struct.unpack_from('<I', blob, o + 4)[0]
            return blob[o + 8:o + 8 + ln]
    return None


def _shader_type(blob):
    c = _chunk(blob)
    if not c or len(c) < 4:
        return None
    return {0: 'PS', 1: 'VS', 2: 'GS', 3: 'HS', 4: 'DS', 5: 'CS'}.get(struct.unpack_from('<I', c, 0)[0] >> 16)


def _operand(t, p):
    """Decode one operand at dword p. Returns (operand, next p). operand = (type, indices, comps, imm)."""
    tok = t[p]
    p += 1
    ncomp = tok & 3
    sel = (tok >> 2) & 3
    cdata = (tok >> 4) & 0xFF
    otype = (tok >> 12) & 0xFF
    idim = (tok >> 20) & 3
    reps = [(tok >> 22) & 7, (tok >> 25) & 7, (tok >> 28) & 7]
    ext = tok >> 31
    while ext:
        ext = t[p] >> 31
        p += 1
    imm = None
    if otype == 4:  # immediate32
        k = 4 if ncomp == 2 else 1
        imm = [struct.unpack('<f', struct.pack('<I', v))[0] for v in t[p:p + k]]
        p += k
    elif otype == 5:  # immediate64
        p += 4 if ncomp == 2 else 2
    idx = []
    for d in range(idim):
        r = reps[d]
        if r == 0:
            idx.append(t[p]); p += 1
        elif r == 1:
            idx.append(None); p += 2
        elif r == 2:
            _, p = _operand(t, p); idx.append(None)
        elif r == 3:
            idx.append(t[p]); p += 1
            _, p = _operand(t, p)
        else:
            idx.append(None); p += 2
            _, p = _operand(t, p)
    comps = None
    if ncomp == 2:
        if sel == 1:
            comps = [(cdata >> (2 * k)) & 3 for k in range(4)]
        elif sel == 2:
            comps = [cdata & 3]
        else:
            comps = [k for k in range(4) if cdata & (1 << k)]
    return (otype, idx, comps, imm), p


MOV, MAD = 0x36, 0x32
CB = 8


def _instructions(code):
    t = struct.unpack('<%dI' % (len(code) // 4), code[:len(code) // 4 * 4])
    p = 2
    while p < len(t):
        tok = t[p]
        op = tok & 0x7FF
        if op == 0x35:  # customdata block
            ln = t[p + 1]
        else:
            ln = (tok >> 24) & 0x7F
        if ln <= 0:
            break
        if op in (MOV, MAD):
            q = p + 1
            ext = tok >> 31
            while ext:
                ext = t[q] >> 31
                q += 1
            ops = []
            try:
                while q < p + ln:
                    o, q = _operand(t, q)
                    ops.append(o)
            except (IndexError, struct.error):
                ops = []
            yield op, ops
        p += ln


def _cb(o):
    """(slot, index, first component) when the operand reads one constant-buffer component."""
    if o[0] == CB and len(o[1]) == 2 and o[1][0] is not None and o[1][1] is not None and o[2]:
        if len(set(o[2])) == 1:
            return o[1][0], o[1][1], o[2][0]
    return None


def roughness_finds(blobs):
    """Every place a pixel shader reads View.RoughnessOverrideParameter-shaped constants: (cb key, kind, value)."""
    finds = []
    for blob in blobs:
        code = _chunk(blob)
        if not code:
            continue
        for op, ops in _instructions(code):
            if op == MAD and len(ops) == 4:
                a, b, c = ops[1], ops[2], ops[3]
                cb_c = _cb(c)
                if not cb_c or cb_c[2] != 0:
                    continue
                for x, y in ((a, b), (b, a)):
                    cy = _cb(y)
                    if cy and cy[:2] == cb_c[:2] and cy[2] == 1:
                        if x[0] == 4 and x[3]:
                            finds.append((cb_c[:2], 'const', x[3][0]))
                        elif x[0] == 0:
                            finds.append((cb_c[:2], 'dynamic', None))
            elif op == MOV and len(ops) == 2:
                s = _cb(ops[1])
                if s and s[2] == 0 and ops[0][2] == [2]:  # r.z = P.x  (roughness folded to 0)
                    finds.append((s[:2], 'zero', None))
    return finds


def override_key(all_finds):
    """The constant-buffer slot/index of the override parameter: the one in the .y/.x mad pattern most often."""
    keys = {}
    for finds in all_finds:
        for k, kind, _ in finds:
            if kind != 'zero':
                keys[k] = keys.get(k, 0) + 1
    return max(keys, key=keys.get) if keys else None


def classify(finds, key):
    """{'kind': 'const', 'value': v} or {'kind': 'dynamic'} or None."""
    kinds = [(kind, v) for k, kind, v in finds if k == key]
    if not kinds:
        return None
    if any(k == 'dynamic' for k, _ in kinds):
        return {'kind': 'dynamic'}
    consts = [v for k, v in kinds if k == 'const']
    if consts:
        return {'kind': 'const', 'value': round(max(0.0, min(1.0, sorted(consts)[len(consts) // 2])), 3)}
    return {'kind': 'const', 'value': 0.0}
