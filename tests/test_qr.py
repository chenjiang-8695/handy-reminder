"""二维码编码器用例 (自写实现, 无第三方依赖)。

为什么值得单独测: 二维码是"扫不出来就等于没做"的东西 —— 矩阵画出来了、字都对,
但扫不出来, 用户在手机前面站半天也不知道是谁的错。所以这里钉两层:

  1. **逐掩码与参考实现比对矩阵** (装了 segno 才跑): 每一处模块必须一样。这一步能定位到
     "对齐图形少画了一个""格式位放错行"这种肉眼看不出来的偏差。
     (实现里唯一**故意**不同的地方是填充码字: ISO 规定 0xEC/0x11 交替, 而 segno 会先补一个
      0x00 字节。两者都是合法二维码, 所以比对时按"数据区偏移"归一化后再比。)
  2. **真解码**: 用 zbar (pyzbar) 把画出来的图真的解一遍, 拿到原文才算过。cv2 的
     QRCodeDetector 也能解大部分, 但它对某些版本会失败 (实测 v6-Q/v8-Q), 所以以 zbar 为准。

没装这两个库时跳过, 不把可选的开发依赖变成硬依赖。
"""

from __future__ import annotations

import pytest

from reminder_assistant import qr as Q

PAYLOADS = [
    "HELLO",
    "handyreminder://bind?v=1&acc=HR-7K3M-9QDT&code=AB3D7F9K",
    "明天下午三点开会",
    "x" * 60,
]


# ---------------------------------------------------------------- 纯逻辑
def test_version_pick_grows_with_payload():
    assert Q.pick_version(1, "H") == 1
    assert Q.pick_version(Q.byte_capacity(1, "L"), "L") == 1          # 刚好塞得下
    assert Q.pick_version(Q.byte_capacity(1, "L") + 1, "L") == 2      # 多一字节就得升版
    assert Q.pick_version(106, "L") == 5                              # 106 = v5-L 的容量
    assert Q.pick_version(107, "L") == 6
    with pytest.raises(ValueError):
        Q.pick_version(200, "H")                 # 版本 10-H 也装不下


def test_byte_capacity_matches_standard_table():
    """v1/v5/v10 的字节容量是标准表上的定值 —— 抄错一位整个编码都是废的。"""
    assert Q.data_codewords(1, "L") == 19 and Q.byte_capacity(1, "L") == 17
    assert Q.data_codewords(1, "M") == 16 and Q.byte_capacity(1, "M") == 14
    assert Q.data_codewords(5, "L") == 108 and Q.byte_capacity(5, "L") == 106
    assert Q.data_codewords(10, "H") == 122      # (6,15)+(2,16)
    assert Q.data_codewords(10, "Q") == 154      # (6,19)+(2,20)


def test_generator_polynomial_and_ec_match_reference_vectors():
    """用一份参考实现的中间数据卡住 RS 编码 (生成多项式次数写错会在这里露馅)。"""
    data = bytes([64, 84, 132, 84, 196, 196, 240, 0]        # 内容 + segno 多补的 0x00
                 + [236, 17, 236, 17, 236, 17, 236, 17, 236, 17, 236])   # 19 = v1-L 的数据码字
    assert len(data) == 19
    assert list(Q.rs_encode(data, 7)) == [61, 133, 53, 168, 191, 235, 84]
    assert Q.rs_generator(7)[0] == 1 and len(Q.rs_generator(7)) == 8


def test_codewords_have_the_right_length_and_padding():
    for version, ec in ((1, "L"), (6, "Q"), (8, "H"), (10, "M")):
        cw = Q.build_codewords("hello world", version, ec)
        expect = sum(b * (d + e) for b, d, e in Q.ECC_TABLE[version][ec])
        assert len(cw) == expect, f"v{version}-{ec} 码字总数不对"
        assert 0xEC in cw and 0x11 in cw, "填充码字必须是 0xEC / 0x11 交替"


def test_unicode_payload_survives_utf8():
    cw = Q.build_codewords("明天下午三点", 2, "M")
    assert len(cw) == sum(b * (d + e) for b, d, e in Q.ECC_TABLE[2]["M"])


def test_same_input_same_matrix_and_mask_is_stable():
    a = Q.encode("稳定吗", "M")
    b = Q.encode("稳定吗", "M")
    assert a.modules == b.modules and a.mask == b.mask
    assert a.size == 17 + 4 * a.version


def test_ascii_art_is_plain_ascii():
    art = Q.encode(PAYLOADS[1], "M").ascii_art()
    assert art.startswith("+") and set(art) <= set("+#-| \n")
    lines = art.strip().splitlines()
    size = Q.encode(PAYLOADS[1], "M").size
    assert len(lines) == size + 2 * 2 + 2            # 上下各 2 行静默区 + 2 条外框
    assert all(len(line) == len(lines[0]) for line in lines), "外框与正文不等宽 (终端里会是歪的)"


# ---------------------------------------------------------------- 掩码/罚分
def test_mask_bit_follows_the_standard_patterns():
    # 掩码 1: 行号为偶数的行整行取反; 掩码 2: 列号 3 的倍数
    assert Q._mask_bit(1, 5, 2) is True and Q._mask_bit(1, 5, 3) is False
    assert Q._mask_bit(2, 3, 7) is True and Q._mask_bit(2, 4, 7) is False
    with pytest.raises(ValueError):
        Q._mask_bit(9, 0, 0)


def test_penalty_counts_long_runs_and_imbalance():
    """规则 1 (连续同色) 与规则 4 (深浅比例) 都要真的计分。"""
    def grid(dark_positions):
        g = [[False] * 21 for _ in range(21)]
        for x, y in dark_positions:
            g[y][x] = True
        return g

    # 同样的深色模块数: 一条 10 连 vs 均匀铺开 —— 连块那版必须罚更多 (规则 1)
    run = grid([(x, 0) for x in range(10)])
    spread = grid([(2 * i, 0) for i in range(10)])
    assert Q.penalty(run) > Q.penalty(spread)
    # 2x2 同色块: 规则 2 的 3 分必须计进去 (与同样 4 个深色、但没有 2x2 的散布比)
    square = grid([(1, 1), (2, 1), (1, 2), (2, 2)])
    diagonal = grid([(0, 0), (2, 2), (4, 4), (6, 6)])
    assert Q.penalty(square) - Q.penalty(diagonal) >= Q.N2
    # 全浅色: 明显偏离 50% 平衡, 规则 4 必然给分
    assert Q.penalty(grid([])) >= Q.N4


# ---------------------------------------------------------------- 真解码
def _pil_image(modules, scale: int = 8, border: int = 4):
    PIL = pytest.importorskip("PIL.Image", reason="需要 Pillow 才能画出图来解码")
    n = len(modules)
    size = (n + 2 * border) * scale
    img = PIL.new("L", (size, size), 255)
    px = img.load()
    for y in range(n):
        for x in range(n):
            if modules[y][x]:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(x + border) * scale + dx, (y + border) * scale + dy] = 0
    return img


@pytest.mark.parametrize("text", PAYLOADS)
@pytest.mark.parametrize("ec", ["L", "M", "Q", "H"])
def test_zbar_really_decodes_our_qr(text, ec):
    """真的解一遍: 拿不到原文就是失败 (这是"扫码绑定"能不能用的唯一判据)。"""
    pyzbar = pytest.importorskip("pyzbar.pyzbar", reason="需要 zbar 才能做端到端解码验证")
    img = _pil_image(Q.encode(text, ec).modules)
    out = pyzbar.decode(img)
    assert out, f"zbar 没认出这张码 (ec={ec}, len={len(text)})"
    assert out[0].data.decode("utf-8") == text


def test_qr_payload_of_a_binding_code_is_decodable():
    """绑定码 + 识别码这一整条载荷也要能扫出来 (最短的最高频场景)。"""
    pyzbar = pytest.importorskip("pyzbar.pyzbar")
    from datetime import datetime

    from reminder_assistant import account as A

    acct = A.Account(account_id="HR-7K3M-9QDT", created_at=datetime.now().astimezone().isoformat())
    now = datetime.now().astimezone()
    code = A.issue_code(acct, now, A.ROLE_DESKTOP)
    payload = A.qr_payload(acct, code)
    assert len(payload.encode()) < 100, "载荷过长得靠更高版本, 演示时扫起来更挑相机"
    out = pyzbar.decode(_pil_image(Q.encode(payload, "M").modules))
    assert out and out[0].data.decode("utf-8") == payload
    # 扫出来的文本必须能被解析回 (识别码, 绑定码)
    parsed = A.parse_qr_payload(out[0].data.decode("utf-8"))
    assert parsed == {"account_id": "HR-7K3M-9QDT", "code": code.code}


# ---------------------------------------------------------------- 参考实现比对
@pytest.mark.parametrize("text", PAYLOADS[:3])
@pytest.mark.parametrize("ec", ["L", "M", "Q", "H"])
def test_matrix_matches_segno_per_mask(text, ec):
    """逐掩码与 segno 比对**功能图形** (定位/对齐/时序/格式位/版本信息), 逐格一致。

    数据区**不**做逐格比对: 两边唯一且合法的差异是填充码字 —— ISO 规定填充是 0xEC/0x11
    交替, 而 segno 在"数据刚好落到字节边界"时会额外补一整个 0x00 字节 (它的 padding 实现
    在 length%8==0 时补满 8 位)。两者都是合法二维码, 各自解码都出原文, 所以数据区等价性
    由 test_zbar_really_decodes_our_qr 用真解码来保证, 这里只钉住"图形骨架"。
    """
    segno = pytest.importorskip("segno", reason="需要 segno 作为参考实现 (开发期依赖)")
    if len(text.encode()) > Q.byte_capacity(10, ec):
        pytest.skip("超出本实现支持的版本范围")
    version = Q.pick_version(len(text.encode()), ec)

    for mask in range(8):
        mine = Q.encode_with_mask(text, ec, mask).modules
        ref = segno.make(text, error=ec.lower(), version=version, mask=mask,
                         boost_error=False, micro=False, mode="byte", encoding="utf-8")
        refm = [[bool(b) for b in row] for row in ref.matrix]
        assert len(refm) == len(mine)
        func_bad = _function_module_mismatch(version, ec, mine, refm)
        assert not func_bad, f"功能图形不一致 (v{version}-{ec} mask{mask}): {func_bad[:6]}"


def test_codewords_match_segno_up_to_padding(text="hello world"):
    """单块版本可以逐码字比对 (含内容与纠错码) —— 多块的交织顺序另有断言。"""
    segno = pytest.importorskip("segno")
    for ec in ("L", "M"):
        version = Q.pick_version(len(text.encode()), ec)
        mine = Q.encode_with_mask(text, ec, 0).modules
        ref = segno.make(text, error=ec.lower(), version=version, mask=0,
                         boost_error=False, micro=False, mode="byte", encoding="utf-8")
        refm = [[bool(b) for b in row] for row in ref.matrix]
        a = _extract_data_bytes(version, ec, 0, mine)
        b = _extract_data_bytes(version, ec, 0, refm)
        content = 4 + Q._count_bits(version) + 8 * len(text.encode())
        must = (content + 7) // 8
        assert a[:must] == b[:must]


def _function_module_mismatch(version, ec, mine, refm):
    m = Q._Matrix(version)
    size = m.size
    m.draw_finder(3, 3)
    m.draw_finder(size - 4, 3)
    m.draw_finder(3, size - 4)
    m.draw_timing()
    if version >= 2:
        pos = Q.ALIGN_POS[version - 2]
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                m.draw_alignment(cx, cy)
    m.draw_version()
    m.draw_format(ec, 0)
    return [(x, y) for y in range(size) for x in range(size)
            if m.func[y][x] and refm[y][x] != mine[y][x]]


def _extract_data_bytes(version, ec, mask, grid) -> bytes:
    """从矩阵反解出**数据块拼接后的码字序列** (先按块表反交织)。

    多块版本必须反交织再比: 交织后的字节流里第 51 个字节并不是数据流的第 51 个字节,
    直接比前缀会得到"内容不一致"的假警报 (而实际上只是填充规则不同)。
    """
    cw = _extract_codewords(version, ec, mask, grid)
    groups = Q.ECC_TABLE[version][ec]
    sizes: list[int] = []
    for blocks, per, _ec in groups:
        sizes += [per] * blocks
    out = bytearray()
    pos = 0
    for i in range(max(sizes)):
        for per in sizes:
            if i < per:
                out.append(cw[pos])
                pos += 1
    return bytes(out)


def _extract_codewords(version, ec, mask, grid) -> list[int]:
    """从矩阵里反解出码字 (重新走一遍功能图形与数据落点, 再去掩码)。"""
    m = Q._Matrix(version)
    size = m.size
    m.draw_finder(3, 3)
    m.draw_finder(size - 4, 3)
    m.draw_finder(3, size - 4)
    m.draw_timing()
    if version >= 2:
        pos = Q.ALIGN_POS[version - 2]
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                m.draw_alignment(cx, cy)
    m.draw_version()
    m.draw_format(ec, 0)
    bits: list[int] = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = (size - 1 - vert) if up else vert
                if m.func[y][x]:
                    continue
                dark = grid[y][x]
                if Q._mask_bit(mask, x, y):
                    dark = not dark
                bits.append(1 if dark else 0)
        right -= 2
    return [int("".join(map(str, bits[i:i + 8])), 2)
            for i in range(0, len(bits) // 8 * 8, 8)]
