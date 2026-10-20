"""极简 QR 码生成 (字节模式, 版本 1-10, 纠错 L/M/Q/H) —— 自带实现, 不依赖第三方库。

为什么自己写: "扫码绑定"(PRD 2.4) 是客户会当场点的功能, 而 qrcode / segno 这类库在客户
机器上不一定装了。少一个可选的运行时依赖, 就少一次"演示当天二维码出不来"。这也是本文件
唯一存在的理由 —— 只做字节模式 + 版本 1-10 (足够放下绑定载荷), 但矩阵必须是**标准 QR**:
tests/test_qr.py 用 segno 逐掩码比对矩阵, 并用 OpenCV 的 QRCodeDetector 真解码一次。

输出的是"模块矩阵"(list[list[bool]]), 渲染交给调用方 (终端用 ASCII, 界面用 QImage,
测试用 Pillow) —— 这样本模块不碰任何图形库, 可以纯离线单测。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------- 标准数据表
# ISO/IEC 18004 附录: version -> 纠错级别 -> ((块数, 每块数据码字数, 每块纠错码字数), ...)
ECC_TABLE: dict[int, dict[str, tuple[tuple[int, int, int], ...]]] = {
    1: {"L": ((1, 19, 7),), "M": ((1, 16, 10),), "Q": ((1, 13, 13),), "H": ((1, 9, 17),)},
    2: {"L": ((1, 34, 10),), "M": ((1, 28, 16),), "Q": ((1, 22, 22),), "H": ((1, 16, 28),)},
    3: {"L": ((1, 55, 15),), "M": ((1, 44, 26),), "Q": ((2, 17, 18),), "H": ((2, 13, 22),)},
    4: {"L": ((1, 80, 20),), "M": ((2, 32, 18),), "Q": ((2, 24, 26),), "H": ((4, 9, 16),)},
    5: {"L": ((1, 108, 26),), "M": ((2, 43, 24),),
        "Q": ((2, 15, 18), (2, 16, 18)), "H": ((2, 11, 22), (2, 12, 22))},
    6: {"L": ((2, 68, 18),), "M": ((4, 27, 16),), "Q": ((4, 19, 24),), "H": ((4, 15, 28),)},
    7: {"L": ((2, 78, 20),), "M": ((4, 31, 18),),
        "Q": ((2, 14, 18), (4, 15, 18)), "H": ((4, 13, 26), (1, 14, 26))},
    8: {"L": ((2, 97, 24),), "M": ((2, 38, 22), (2, 39, 22)),
        "Q": ((4, 18, 22), (2, 19, 22)), "H": ((4, 14, 26), (2, 15, 26))},
    9: {"L": ((2, 116, 30),), "M": ((3, 36, 22), (2, 37, 22)),
        "Q": ((4, 16, 20), (4, 17, 20)), "H": ((4, 12, 24), (4, 13, 24))},
    10: {"L": ((2, 68, 18), (2, 69, 18)), "M": ((4, 43, 26), (1, 44, 26)),
         "Q": ((6, 19, 24), (2, 20, 24)), "H": ((6, 15, 28), (2, 16, 28))},
}

# 对齐图形中心坐标 (版本 1 没有), 索引 = 版本 - 2
ALIGN_POS: tuple[tuple[int, ...], ...] = (
    (6, 18), (6, 22), (6, 26), (6, 30), (6, 34),          # 2..6
    (6, 22, 38), (6, 24, 42), (6, 26, 46), (6, 28, 50),   # 7..10
)

# 纠错级别 -> 格式信息里的 2 bit 编码
EC_BITS: dict[str, int] = {"L": 1, "M": 0, "Q": 3, "H": 2}
# 剩余位 (版本 1-10 只有 2..6 有 7 位)
REMAINDER_BITS: dict[int, int] = {v: 7 for v in range(2, 7)}

MAX_VERSION = 10

# ---------------------------------------------------------------- GF(256)


def _build_galois():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D                     # 本原多项式 x^8+x^4+x^3+x^2+1
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


GALOIS_EXP, GALOIS_LOG = _build_galois()
_GEN_CACHE: dict[int, list[int]] = {}


def rs_generator(degree: int) -> list[int]:
    """生成多项式 g(x) = (x-α^0)(x-α^1)…(x-α^(degree-1))。

    返回 degree+1 个系数, **最高次在前**(首项恒为 1)。逐次乘一个一次因式 ——
    一次性展开很容易把次数写错一位, 而次数写错的症状是"矩阵能画出来、就是扫不出来"。
    """
    if degree in _GEN_CACHE:
        return _GEN_CACHE[degree]
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef                                  # x * poly
            nxt[j + 1] ^= _mul(coef, GALOIS_EXP[i])          # α^i * poly
        poly = nxt
    _GEN_CACHE[degree] = poly
    return poly


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return GALOIS_EXP[GALOIS_LOG[a] + GALOIS_LOG[b]]


def rs_encode(data: bytes, ec_len: int) -> bytes:
    """Reed-Solomon 纠错码字 (综合除法)。"""
    gen = rs_generator(ec_len)
    res = bytearray(data) + bytearray(ec_len)
    for i in range(len(data)):
        factor = res[i]
        if factor == 0:
            continue
        for j in range(1, len(gen)):
            res[i + j] ^= _mul(gen[j], factor)
    return bytes(res[len(data):])


# ---------------------------------------------------------------- 位流


def _append_bits(bits: list[int], value: int, length: int) -> None:
    for i in range(length - 1, -1, -1):
        bits.append((value >> i) & 1)


def data_codewords(version: int, ec: str) -> int:
    return sum(blocks * per for blocks, per, _ in ECC_TABLE[version][ec])


def _count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


def byte_capacity(version: int, ec: str) -> int:
    """这个版本+纠错级别最多能装多少字节 (已扣掉模式指示符与长度字段)。"""
    return (data_codewords(version, ec) * 8 - 4 - _count_bits(version)) // 8


def pick_version(nbytes: int, ec: str = "M", min_version: int = 1) -> int:
    for v in range(max(1, min_version), MAX_VERSION + 1):
        if nbytes <= byte_capacity(v, ec):
            return v
    raise ValueError(f"内容太长: {nbytes} 字节, 版本 {MAX_VERSION} 装不下 (纠错 {ec})")


def _payload_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def build_codewords(text: str, version: int, ec: str) -> bytes:
    """文本 -> 最终码字序列 (数据块交织 + 纠错块交织)。"""
    raw = _payload_bytes(text)
    groups = ECC_TABLE[version][ec]
    capacity = data_codewords(version, ec)
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)                     # 字节模式
    _append_bits(bits, len(raw), _count_bits(version))
    for b in raw:
        _append_bits(bits, b, 8)
    if len(bits) > capacity * 8:
        raise ValueError(f"内容放不进版本 {version} (纠错 {ec})")
    # 终止符: 最多 4 个 0, 但不能超出容量
    bits.extend([0] * min(4, capacity * 8 - len(bits)))
    bits.extend([0] * (-len(bits) % 8))               # 补齐到整字节
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < capacity * 8:
        _append_bits(bits, pad[i % 2], 8)
        i += 1
    words = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]

    data_blocks: list[bytes] = []
    ec_blocks: list[bytes] = []
    pos = 0
    for blocks, per, ec_per in groups:
        for _ in range(blocks):
            block = bytes(words[pos:pos + per])
            pos += per
            data_blocks.append(block)
            ec_blocks.append(rs_encode(block, ec_per))
    out = bytearray()
    for i in range(max(len(b) for b in data_blocks)):
        for b in data_blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(max(len(b) for b in ec_blocks)):
        for b in ec_blocks:
            if i < len(b):
                out.append(b[i])
    return bytes(out)


# ---------------------------------------------------------------- 矩阵


@dataclass
class QrCode:
    version: int
    ec: str
    size: int
    modules: list[list[bool]]         # modules[y][x] == True 表示黑
    mask: int = 0

    def ascii_art(self, border: int = 2) -> str:
        """终端里能看的二维码 (纯 ASCII: 中文 Windows 控制台是 GBK, 别用 Unicode 方块字符)。

        边框宽度必须按"每个模块 2 个字符"来算 —— 少算一半会让外框比正文窄, 终端里看着
        就是一张歪掉的码; 而且这种错不影响解码 (图是给眼睛看的), 所以只有断言宽度才发现得了。
        """
        inner = 2 * self.size + 2 * border
        line = "+" + "-" * inner + "+"
        rows = [line]
        for _ in range(border):
            rows.append("|" + " " * inner + "|")
        for y in range(self.size):
            body = "".join("##" if self.modules[y][x] else "  " for x in range(self.size))
            rows.append("|" + " " * border + body + " " * border + "|")
        for _ in range(border):
            rows.append("|" + " " * inner + "|")
        rows.append(line)
        return "\n".join(rows)

    def rows_of_bools(self) -> list[list[bool]]:
        return self.modules


class _Matrix:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.func[y][x] = True

    def draw_finder(self, cx: int, cy: int) -> None:
        """7x7 定位图形 + 1 模块白色分隔区 (半径 3 的方框, 边界外一圈留白)。"""
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if not (0 <= x < self.size and 0 <= y < self.size):
                    continue
                dist = max(abs(dx), abs(dy))
                self.set(x, y, dist != 2 and dist != 4)

    def draw_alignment(self, cx: int, cy: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def draw_timing(self) -> None:
        for i in range(8, self.size - 8):
            dark = (i % 2 == 0)
            self.set(6, i, dark)
            self.set(i, 6, dark)

    def draw_format(self, ec: str, mask: int) -> None:
        data = (EC_BITS[ec] << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: ((bits >> i) & 1) == 1        # noqa: E731
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(self.size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, self.size - 15 + i, bit(i))
        self.set(8, self.size - 8, True)              # 固定的黑模块

    def draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            b = ((bits >> i) & 1) == 1
            a = self.size - 11 + i % 3
            row = i // 3
            self.set(a, row, b)
            self.set(row, a, b)

    def draw_data(self, codewords: bytes) -> None:
        bit_index = 0
        total = len(codewords) * 8
        right = self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (self.size - 1 - vert) if upward else vert
                    if self.func[y][x] or bit_index >= total:
                        continue
                    dark = ((codewords[bit_index >> 3] >> (7 - (bit_index & 7))) & 1) == 1
                    self.modules[y][x] = dark
                    bit_index += 1
            right -= 2

    def apply_mask(self, mask: int) -> list[list[bool]]:
        out = [row[:] for row in self.modules]
        for y in range(self.size):
            for x in range(self.size):
                if self.func[y][x]:
                    continue
                if _mask_bit(mask, x, y):
                    out[y][x] = not out[y][x]
        return out


def _mask_bit(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (y // 2 + x // 3) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    if mask == 7:
        return ((x + y) % 2 + x * y % 3) % 2 == 0
    raise ValueError(f"掩码只能是 0-7: {mask}")


# ---- 掩码罚分 (ISO/IEC 18004 7.8.3.1) ----
N1, N2, N3, N4 = 3, 3, 40, 10

# 规则 3: 形如 1:1:3:1:1 (深:浅:深:浅:深) 且某一侧有 4 个浅色模块的图形。
# 直接在每行/每列上找这两个位串 —— 比维护"游程历史 + 幻影哨兵"的实现好读得多,
# 也不会出现"差一格的哨兵索引"这种只有对比参考实现才看得出来的偏差。
_PAT_A = "10111010000"
_PAT_B = "00001011101"


def penalty(modules: list[list[bool]]) -> int:
    size = len(modules)
    score = 0
    lines = [list(row) for row in modules] + [list(col) for col in zip(*modules)]
    for line in lines:
        text = "".join("1" if c else "0" for c in line)
        run_len = 1
        for i in range(1, len(line)):
            if line[i] == line[i - 1]:
                run_len += 1
                if run_len == 5:
                    score += N1
                elif run_len > 5:
                    score += 1
            else:
                run_len = 1
        padded = "0000" + text + "0000"      # 符号外的静默区也算浅色 (两边各留 4 格)
        for pat in (_PAT_A, _PAT_B):
            start = padded.find(pat)
            while start != -1:
                score += N3
                start = padded.find(pat, start + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            c = modules[y][x]
            if c == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                score += N2
    dark = sum(sum(1 for c in row if c) for row in modules)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    score += max(0, k) * N4
    return score


def encode(text: str, ec: str = "M", *, min_version: int = 1,
           mask: int | None = None) -> QrCode:
    """文本 -> QR 矩阵。ec 为纠错级别, mask 一般留空 (自动按罚分挑最优)。"""
    if ec not in ECC_TABLE[1]:
        raise ValueError(f"纠错级别只能是 L/M/Q/H: {ec}")
    payload = _payload_bytes(text)
    version = pick_version(len(payload), ec, min_version)
    return encode_at(text, version, ec, mask=mask)


def encode_at(text: str, version: int, ec: str = "M", *, mask: int | None = None) -> QrCode:
    if version not in ECC_TABLE:
        raise ValueError(f"只支持版本 1-{MAX_VERSION}: {version}")
    codewords = build_codewords(text, version, ec)
    m = _Matrix(version)
    m.draw_finder(3, 3)
    m.draw_finder(m.size - 4, 3)
    m.draw_finder(3, m.size - 4)
    m.draw_timing()
    if version >= 2:
        pos = ALIGN_POS[version - 2]
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                # 只跳过与定位图形重叠的三个角。**不能**用"这个点已经是功能模块就跳过":
                # 贴边那几个对齐图形的中心正好落在时序行/列上(它们本来就该盖住时序图案),
                # 按"已占用"去判会把它们全漏掉 —— 症状正是"矩阵画得出来、扫不出来",
                # 而且只在带对齐图形的版本上出现。
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                m.draw_alignment(cx, cy)
    m.draw_version()
    # 格式信息那 15×2 个格子先占位 (用假掩码): 它们不参与数据填充。
    # 顺序不能反 —— 先填数据再画格式位, 数据就会写进格式区, 扫出来是乱码。
    m.draw_format(ec, 0)
    m.draw_data(codewords)
    candidates = [mask] if mask is not None else list(range(8))
    best: tuple[int, int, list[list[bool]]] | None = None
    for mk in candidates:
        grid = m.apply_mask(mk)                       # 掩码只作用于数据格
        m.draw_format(ec, mk)                         # 再写真实的格式位
        for y in range(m.size):                       # 功能图形不参与掩码, 原样盖回去
            for x in range(m.size):
                if m.func[y][x]:
                    grid[y][x] = m.modules[y][x]
        sc = penalty(grid)
        if best is None or sc < best[0]:
            best = (sc, mk, grid)
    assert best is not None
    return QrCode(version=version, ec=ec, size=m.size, modules=best[2], mask=best[1])


def encode_with_mask(text: str, ec: str = "M", mask: int = 0, *, version: int | None = None) -> QrCode:
    """固定掩码/版本 —— 主要用于与参考实现逐掩码比对矩阵 (测试用)。"""
    v = version or pick_version(len(_payload_bytes(text)), ec)
    return encode_at(text, v, ec, mask=mask)
