import hashlib
import os
import struct

PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41,
    0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
    0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
])

def rc4(key, data):
    if not key or not data:
        return bytes(data or b"")
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for k, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16")

_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF

def _gmul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r

_M9 = [_gmul(i, 9) for i in range(256)]
_M11 = [_gmul(i, 11) for i in range(256)]
_M13 = [_gmul(i, 13) for i in range(256)]
_M14 = [_gmul(i, 14) for i in range(256)]
_M2 = [_gmul(i, 2) for i in range(256)]
_M3 = [_gmul(i, 3) for i in range(256)]

def _expand_key(key):
    nk = len(key) // 4
    if nk not in (4, 6, 8):
        raise ValueError("AES 密钥长度必须是 16/24/32 字节，收到 %d" % len(key))
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    return [bytes(sum(w[4 * r:4 * r + 4], [])) for r in range(nr + 1)]

def _add_round_key(state, rk):
    return bytes(a ^ b for a, b in zip(state, rk))

def _inv_shift_rows(s):
    return bytes([
        s[0], s[13], s[10], s[7],
        s[4], s[1], s[14], s[11],
        s[8], s[5], s[2], s[15],
        s[12], s[9], s[6], s[3],
    ])

def _inv_mix_columns(s):
    out = bytearray(16)
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        out[i] = _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]
        out[i + 1] = _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]
        out[i + 2] = _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]
        out[i + 3] = _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]
    return bytes(out)

def _decrypt_block(rk, block):
    state = _add_round_key(block, rk[-1])
    for r in range(len(rk) - 2, 0, -1):
        state = bytes(_INV_SBOX[b] for b in state)
        state = _inv_shift_rows(state)
        state = _add_round_key(state, rk[r])
        state = _inv_mix_columns(state)
    state = bytes(_INV_SBOX[b] for b in state)
    state = _inv_shift_rows(state)
    return _add_round_key(state, rk[0])

def _shift_rows(s):
    return bytes([
        s[0], s[5], s[10], s[15],
        s[4], s[9], s[14], s[3],
        s[8], s[13], s[2], s[7],
        s[12], s[1], s[6], s[11],
    ])

def _mix_columns(s):
    out = bytearray(16)
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        out[i] = _M2[a0] ^ _M3[a1] ^ a2 ^ a3
        out[i + 1] = a0 ^ _M2[a1] ^ _M3[a2] ^ a3
        out[i + 2] = a0 ^ a1 ^ _M2[a2] ^ _M3[a3]
        out[i + 3] = _M3[a0] ^ a1 ^ a2 ^ _M2[a3]
    return bytes(out)

def _encrypt_block(rk, block):
    state = _add_round_key(block, rk[0])
    for r in range(1, len(rk) - 1):
        state = bytes(_SBOX[b] for b in state)
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, rk[r])
    state = bytes(_SBOX[b] for b in state)
    state = _shift_rows(state)
    return _add_round_key(state, rk[-1])

def aes_cbc_decrypt(key, data):
    if len(data) < 32:
        return b""
    return _strip_padding(aes_cbc_decrypt_raw(key, data[16:], data[:16]))

def _pad_password(pw):
    pw = pw[:32]
    return pw + PAD[:32 - len(pw)]

def string_bytes(v):
    if isinstance(v, bytes):
        if v.startswith(b"("):
            return _decode_literal(v[1:-1])
        if v.startswith(b"<") and not v.startswith(b"<<"):
            return _from_hex(v[1:-1])
        return v
    if isinstance(v, str):
        return v.encode("latin-1", "replace")
    return b""

_as_bytes = string_bytes

def _from_hex(raw):
    hx = bytes(c for c in raw if c in b"0123456789abcdefABCDEF")
    if len(hx) % 2:
        hx += b"0"
    try:
        return bytes.fromhex(hx.decode("ascii"))
    except ValueError:
        return b""

def _decode_literal(raw):
    out = bytearray()
    i = 0
    n = len(raw)
    simple = {ord("n"): 0x0A, ord("r"): 0x0D, ord("t"): 0x09,
              ord("b"): 0x08, ord("f"): 0x0C,
              ord("("): 0x28, ord(")"): 0x29, ord("\\"): 0x5C}
    while i < n:
        c = raw[i]
        if c != 0x5C:
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        c = raw[i]
        if c in simple:
            out.append(simple[c])
            i += 1
        elif 0x30 <= c <= 0x37:
            j = i
            val = 0
            while j < n and j - i < 3 and 0x30 <= raw[j] <= 0x37:
                val = val * 8 + (raw[j] - 0x30)
                j += 1
            out.append(val & 0xFF)
            i = j
        elif c == 0x0D:
            i += 1
            if i < n and raw[i] == 0x0A:
                i += 1
        elif c == 0x0A:
            i += 1
        else:
            out.append(c)
            i += 1
    return bytes(out)

def _file_key_r2_r4(password, o, p, id0, r, nbytes, encrypt_metadata):
    buf = _pad_password(password) + o
    buf += (p & 0xFFFFFFFF).to_bytes(4, "little")
    buf += id0
    if r >= 4 and not encrypt_metadata:
        buf += b"\xff\xff\xff\xff"
    key = hashlib.md5(buf).digest()
    if r >= 3:
        for _ in range(50):
            key = hashlib.md5(key[:nbytes]).digest()
    return key[:nbytes]

def _u_value(key, id0, r):
    if r == 2:
        return rc4(key, PAD)
    h = hashlib.md5(PAD + id0).digest()
    out = rc4(key, h)
    for i in range(1, 20):
        out = rc4(bytes(b ^ i for b in key), out)
    return out

def _o_value_r3(owner_pw, user_pw, r, nbytes):
    key = hashlib.md5(_pad_password(owner_pw)).digest()
    if r >= 3:
        for _ in range(50):
            key = hashlib.md5(key).digest()
    key = key[:nbytes]
    out = rc4(key, _pad_password(user_pw))
    if r >= 3:
        for i in range(1, 20):
            out = rc4(bytes(b ^ i for b in key), out)
    return out

def _user_pw_from_owner(owner_pw, o, r, nbytes):
    if r == 2:
        key = hashlib.md5(_pad_password(owner_pw)).digest()[:nbytes]
        return rc4(key, o)
    key = hashlib.md5(_pad_password(owner_pw)).digest()
    for _ in range(50):
        key = hashlib.md5(key).digest()
    key = key[:nbytes]
    out = o
    for i in range(19, 0, -1):
        out = rc4(bytes(b ^ i for b in key), out)
    out = rc4(key, out)
    return out

def _hash_2b(password, salt, udata=b""):
    k = hashlib.sha256(password + salt + udata).digest()
    e = b"\x00" * 16
    i = 0
    while i < 64 or e[-1] > i - 32:
        k1 = (password + k + udata) * 64
        e = aes_cbc_encrypt_nopad(k[:16], k[16:32], k1)
        mod = sum(e[:16]) % 3
        if mod == 0:
            k = hashlib.sha256(e).digest()
        elif mod == 1:
            k = hashlib.sha384(e).digest()
        else:
            k = hashlib.sha512(e).digest()
        i += 1
    return k[:32]

def _hash_2a(password, salt, udata=b"", r=6):
    if r == 5:
        return hashlib.sha256(password + salt + udata).digest()
    return _hash_2b(password, salt, udata)

class StandardSecurity:

    def __init__(self, enc, file_id=b"", password=b""):
        self.v = _int(enc.get("V"), 0)
        self.r = _int(enc.get("R"), 0)
        self.o = _as_bytes(enc.get("O"))
        self.u = _as_bytes(enc.get("U"))
        self.p = _int(enc.get("P"), 0)
        self.oe = _as_bytes(enc.get("OE"))
        self.ue = _as_bytes(enc.get("UE"))
        self.perms = _as_bytes(enc.get("Perms"))
        self.encrypt_metadata = enc.get("EncryptMetadata") is not False
        self.id0 = file_id

        if self.r == 2:
            self.nbytes = 5
        elif self.r >= 5:
            self.nbytes = 32
        else:
            bits = _int(enc.get("Length"), 40)
            self.nbytes = max(5, min(16, bits // 8))

        self.cfm_stm, self.cfm_str = self._pick_cfm(enc)
        self.key = None
        self.ok = False
        self.reason = ""
        self.used_owner = False
        self._compute(password)

    def _pick_cfm(self, enc):
        if self.v < 4:
            return ("V2", "V2")
        cf = enc.get("CF") or {}

        def resolve(name):
            name = str(name or "Identity")
            if name == "Identity":
                return "None"
            d = cf.get(name) if isinstance(cf, dict) else None
            if isinstance(d, dict):
                cfm = str(d.get("CFM", "None"))
                if cfm in ("V2", "AESV2", "AESV3"):
                    return cfm
            return "None"

        return (resolve(enc.get("StmF")), resolve(enc.get("StrF")))

    def _compute(self, password):
        if self.r >= 5:
            self._compute_aes256(password)
            return
        if not self.o or not self.u:
            self.reason = "加密字典缺 /O 或 /U"
            return

        key = _file_key_r2_r4(password, self.o, self.p, self.id0,
                              self.r, self.nbytes, self.encrypt_metadata)
        if self._verify_user(key):
            self.key, self.ok = key, True
            return

        if len(self.o) >= 32:
            upad = _user_pw_from_owner(password, self.o, self.r, self.nbytes)
            key2 = _file_key_r2_r4(upad, self.o, self.p, self.id0,
                                   self.r, self.nbytes, self.encrypt_metadata)
            if self._verify_user(key2):
                self.key, self.ok, self.used_owner = key2, True, True
                return
        self.reason = "口令不正确"

    def _verify_user(self, key):
        want = _u_value(key, self.id0, self.r)
        if self.r == 2:
            return self.u[:32] == want
        return self.u[:16] == want[:16]

    def _compute_aes256(self, password):
        if len(self.u) < 48 or len(self.ue) < 32:
            self.reason = "AES-256 的 /U 或 /UE 长度不对"
            return
        pw = password[:127]

        v_salt, k_salt = self.u[32:40], self.u[40:48]
        if _hash_2a(pw, v_salt, b"", self.r) == self.u[:32]:
            h = _hash_2a(pw, k_salt, b"", self.r)
            self.key, self.ok = aes_cbc_decrypt_raw(h, self.ue[:32])[:32], True
            return

        if len(self.o) >= 48 and len(self.oe) >= 32:
            ov_salt, ok_salt = self.o[32:40], self.o[40:48]
            if _hash_2a(pw, ov_salt, self.u[:48], self.r) == self.o[:32]:
                h = _hash_2a(pw, ok_salt, self.u[:48], self.r)
                self.key = aes_cbc_decrypt_raw(h, self.oe[:32])[:32]
                self.ok, self.used_owner = True, True
                return
        self.reason = "口令不正确"

    def object_key(self, num, gen, is_stream):
        if self.r >= 5:
            return self.key
        cfm = self.cfm_stm if is_stream else self.cfm_str
        buf = self.key
        buf += struct.pack("<i", num)[:3]
        buf += struct.pack("<i", gen)[:2]
        if cfm in ("AESV2", "AESV3"):
            buf += b"sAlT"
        return hashlib.md5(buf).digest()[:min(self.nbytes + 5, 16)]

    def decrypt(self, num, gen, data, is_stream):
        if not data or not self.ok:
            return data
        cfm = self.cfm_stm if is_stream else self.cfm_str
        if cfm == "None":
            return data
        key = self.object_key(num, gen, is_stream)
        if cfm == "V2":
            return rc4(key, data)
        if len(data) < 32:
            return b""
        plain = aes_cbc_decrypt(key, data)
        return _strip_padding(plain)

def _strip_padding(data):
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and len(data) >= n:
        if all(b == n for b in data[-n:]):
            return data[:-n]
    return data

def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _rot32(w, n):
    return ((w >> n) | (w << (32 - n))) & 0xFFFFFFFF

def _build_tables():
    te0 = [0] * 256
    td0 = [0] * 256
    for x in range(256):
        s = _SBOX[x]
        te0[x] = ((_gmul(s, 2) << 24) | (s << 16) | (s << 8) | _gmul(s, 3))
        si = _INV_SBOX[x]
        td0[x] = ((_gmul(si, 14) << 24) | (_gmul(si, 9) << 16)
                  | (_gmul(si, 13) << 8) | _gmul(si, 11))
    te = (te0, [_rot32(w, 8) for w in te0],
          [_rot32(w, 16) for w in te0], [_rot32(w, 24) for w in te0])
    td = (td0, [_rot32(w, 8) for w in td0],
          [_rot32(w, 16) for w in td0], [_rot32(w, 24) for w in td0])
    return te, td

_TE, _TD = _build_tables()

def _word_key(key):
    words = []
    for r in _expand_key(key):
        words.append(int.from_bytes(r[0:4], "big"))
        words.append(int.from_bytes(r[4:8], "big"))
        words.append(int.from_bytes(r[8:12], "big"))
        words.append(int.from_bytes(r[12:16], "big"))
    return words

def _inv_mix_word(w):
    a0, a1, a2, a3 = w >> 24, (w >> 16) & 0xFF, (w >> 8) & 0xFF, w & 0xFF
    return (((_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]) << 24)
            | ((_M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]) << 16)
            | ((_M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]) << 8)
            | (_M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]))

def _decrypt_keys(key):
    w = _word_key(key)
    nr = len(w) // 4 - 1
    out = list(w)
    for r in range(1, nr):
        for j in range(4):
            out[4 * r + j] = _inv_mix_word(w[4 * r + j])
    return out

def _encrypt_block_te(rk, block):
    te0, te1, te2, te3 = _TE
    s = list(int.from_bytes(block[i:i + 4], "big") for i in range(0, 16, 4))
    s = [s[i] ^ rk[i] for i in range(4)]
    nr = len(rk) // 4 - 1
    for r in range(1, nr):
        i = 4 * r
        s0, s1, s2, s3 = s
        s = [
            te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ rk[i],
            te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ rk[i + 1],
            te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ rk[i + 2],
            te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ rk[i + 3],
        ]
    i = 4 * nr
    s0, s1, s2, s3 = s
    sb = _SBOX
    out = [
        ((sb[s0 >> 24] << 24) | (sb[(s1 >> 16) & 0xFF] << 16)
         | (sb[(s2 >> 8) & 0xFF] << 8) | sb[s3 & 0xFF]) ^ rk[i],
        ((sb[s1 >> 24] << 24) | (sb[(s2 >> 16) & 0xFF] << 16)
         | (sb[(s3 >> 8) & 0xFF] << 8) | sb[s0 & 0xFF]) ^ rk[i + 1],
        ((sb[s2 >> 24] << 24) | (sb[(s3 >> 16) & 0xFF] << 16)
         | (sb[(s0 >> 8) & 0xFF] << 8) | sb[s1 & 0xFF]) ^ rk[i + 2],
        ((sb[s3 >> 24] << 24) | (sb[(s0 >> 16) & 0xFF] << 16)
         | (sb[(s1 >> 8) & 0xFF] << 8) | sb[s2 & 0xFF]) ^ rk[i + 3],
    ]
    return b"".join(x.to_bytes(4, "big") for x in out)

def _decrypt_block_td(rk, block):
    td0, td1, td2, td3 = _TD
    s = [int.from_bytes(block[i:i + 4], "big") for i in range(0, 16, 4)]
    nr = len(rk) // 4 - 1
    i = 4 * nr
    s = [s[0] ^ rk[i], s[1] ^ rk[i + 1], s[2] ^ rk[i + 2], s[3] ^ rk[i + 3]]
    for r in range(nr - 1, 0, -1):
        i = 4 * r
        s0, s1, s2, s3 = s
        s = [
            td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ rk[i],
            td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ rk[i + 1],
            td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ rk[i + 2],
            td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ rk[i + 3],
        ]
    si = _INV_SBOX
    s0, s1, s2, s3 = s
    out = [
        ((si[s0 >> 24] << 24) | (si[(s3 >> 16) & 0xFF] << 16)
         | (si[(s2 >> 8) & 0xFF] << 8) | si[s1 & 0xFF]) ^ rk[0],
        ((si[s1 >> 24] << 24) | (si[(s0 >> 16) & 0xFF] << 16)
         | (si[(s3 >> 8) & 0xFF] << 8) | si[s2 & 0xFF]) ^ rk[1],
        ((si[s2 >> 24] << 24) | (si[(s1 >> 16) & 0xFF] << 16)
         | (si[(s0 >> 8) & 0xFF] << 8) | si[s3 & 0xFF]) ^ rk[2],
        ((si[s3 >> 24] << 24) | (si[(s2 >> 16) & 0xFF] << 16)
         | (si[(s1 >> 8) & 0xFF] << 8) | si[s0 & 0xFF]) ^ rk[3],
    ]
    return b"".join(x.to_bytes(4, "big") for x in out)

_ENC_KEY_CACHE = {}
_DEC_KEY_CACHE = {}

def _enc_keys(key):
    k = bytes(key)
    if k not in _ENC_KEY_CACHE:
        if len(_ENC_KEY_CACHE) > 64:
            _ENC_KEY_CACHE.clear()
        _ENC_KEY_CACHE[k] = _word_key(k)
    return _ENC_KEY_CACHE[k]

def _dec_keys(key):
    k = bytes(key)
    if k not in _DEC_KEY_CACHE:
        if len(_DEC_KEY_CACHE) > 64:
            _DEC_KEY_CACHE.clear()
        _DEC_KEY_CACHE[k] = _decrypt_keys(k)
    return _DEC_KEY_CACHE[k]

def aes_cbc_encrypt_nopad(key, iv, data):
    rk = _enc_keys(key)
    body = data[:len(data) - (len(data) % 16)]
    out = bytearray()
    prev = iv
    for i in range(0, len(body), 16):
        blk = bytes(a ^ b for a, b in zip(body[i:i + 16], prev))
        blk = _encrypt_block_te(rk, blk)
        out += blk
        prev = blk
    return bytes(out)

def aes_cbc_decrypt_raw(key, data, iv=None):
    rk = _dec_keys(key)
    if iv is None:
        iv = b"\x00" * 16
    body = data[:len(data) - (len(data) % 16)]
    out = bytearray()
    prev = iv
    for i in range(0, len(body), 16):
        blk = body[i:i + 16]
        plain = _decrypt_block_td(rk, blk)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = blk
    return bytes(out)

ZERO_IV = b"\x00" * 16

MAX_PASSWORD_BYTES = 127

PERM_PRINT = 4
PERM_MODIFY = 8
PERM_COPY = 16
PERM_ANNOT = 32
PERM_FILL = 256
PERM_EXTRACT = 512
PERM_ASSEMBLE = 1024
PERM_PRINT_HQ = 2048

PERM_ALL = 0xFFFFFFFC

def _signed32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v

def make_permissions(allow_print=True, allow_copy=True, allow_modify=True):
    p = PERM_ALL
    if not allow_print:
        p &= ~(PERM_PRINT | PERM_PRINT_HQ)
    if not allow_copy:
        p &= ~(PERM_COPY | PERM_EXTRACT)
    if not allow_modify:
        p &= ~(PERM_MODIFY | PERM_ANNOT | PERM_FILL | PERM_ASSEMBLE)
    return _signed32(p)

def _pw_bytes(p):
    if isinstance(p, str):
        p = p.encode("utf-8")
    return bytes(p or b"")[:127]

def _pkcs5_pad(data):
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n

def aes_cbc_encrypt(key, data):
    iv = os.urandom(16)
    return iv + aes_cbc_encrypt_nopad(key, iv, _pkcs5_pad(data))

class SecurityEncoder:

    def __init__(self, user_pw=b"", owner_pw=b"", perms=None,
                 algorithm="aes256", id0=None, encrypt_metadata=True):
        self.encrypt_metadata = bool(encrypt_metadata)
        self.id0 = bytes(id0 or os.urandom(16))
        self.p = _signed32(PERM_ALL if perms is None else perms)

        algo = str(algorithm or "").lower().replace("_", "").replace("-", "")
        self.r = 4 if algo in ("aes128", "r4", "aesv2") else 6
        self.v = 4 if self.r == 4 else 5
        self.nbytes = 16 if self.r == 4 else 32
        self.cfm = "AESV2" if self.r == 4 else "AESV3"

        self.file_key = b""
        self.o = self.u = self.oe = self.ue = self.perms = b""

        user = _pw_bytes(user_pw)
        owner = _pw_bytes(owner_pw) or user
        if self.r == 4:
            self._build_r4(user, owner)
        else:
            self._build_r6(user, owner)

    def _build_r6(self, user, owner):
        key = os.urandom(32)
        vsalt, ksalt = os.urandom(8), os.urandom(8)
        self.u = _hash_2a(user, vsalt, b"", 6) + vsalt + ksalt
        self.ue = aes_cbc_encrypt_nopad(
            _hash_2a(user, ksalt, b"", 6), ZERO_IV, key)

        ovsalt, oksalt = os.urandom(8), os.urandom(8)
        self.o = _hash_2a(owner, ovsalt, self.u, 6) + ovsalt + oksalt
        self.oe = aes_cbc_encrypt_nopad(
            _hash_2a(owner, oksalt, self.u, 6), ZERO_IV, key)

        flag = b"T" if self.encrypt_metadata else b"F"
        blob = ((self.p & 0xFFFFFFFF).to_bytes(4, "little")
                + b"\xff\xff\xff\xff" + flag + b"adb" + os.urandom(4))
        self.perms = aes_cbc_encrypt_nopad(key, ZERO_IV, blob)
        self.file_key = key

    def _build_r4(self, user, owner):
        self.o = _o_value_r3(owner, user, 4, 16)
        key = _file_key_r2_r4(user, self.o, self.p, self.id0, 4, 16,
                              self.encrypt_metadata)
        self.u = _u_value(key, self.id0, 4) + os.urandom(16)
        self.file_key = key

    def object_key(self, num, gen, is_stream):
        if self.r >= 5:
            return self.file_key
        buf = (self.file_key
               + struct.pack("<i", num)[:3]
               + struct.pack("<i", gen)[:2])
        return hashlib.md5(buf + b"sAlT").digest()[:16]

    def encrypt(self, num, gen, data, is_stream):
        if not data:
            return b""
        return aes_cbc_encrypt(self.object_key(num, gen, is_stream), data)
