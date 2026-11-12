#!/usr/bin/env python3
"""重新给应用图标换底色：把「白字压彩色圆角方块」的底板换成另一种颜色。

    python3 scripts/gen-app-icon.py --to "#1A1A1A"
    python3 scripts/gen-app-icon.py --to "#1A1A1A" --check     # 只报精度，不写文件

## 为什么需要它

应用图标的三个副本（`wind_tsf/res/wind_input.ico`、`assets/installer.ico`、
wind-setting 仓的 `res/wind_setting.ico`）是**同一份二进制**，字形（白色「风」）
逐尺寸手调过。换底板颜色时不能重新渲染字形——那会把手工调过的每一档都毁掉，
16px 那档尤其经不起重画。所以做法是**按像素反解出字形覆盖度，再重新合成**：
底板的形状、圆角、抗锯齿边缘、字形轮廓全部原样保留，只有底色变。

## 原理

原图每个像素都是「底板色 B ↔ 字形色 W」这条线段的混色：

    P = B·(1 − c) + W·c      c ∈ [0,1] 是字形覆盖度

故把 c 投影到 B→W 方向即可反解：

    c = ⟨P − B, W − B⟩ / ‖W − B‖²        （夹到 [0,1]）

再以新底色 B′ 重新合成 `P′ = B′·(1 − c) + W·c`，alpha **完全沿用原像素**，
于是圆角与抗锯齿边缘一位不改。

投影（而不是逐通道均值）是必要的：B 与 W 的 R 通道差高达 210，而 G 只差 109，
逐通道平均会让偏色像素算错覆盖度、边缘出现彩色毛边。实测投影模型下
全图像素往返误差中位数 1/255、p95 为 3/255。

`--check` 会打印误差分布，换底板色前可以先跑一遍确认这仍是一条线段混色
（误差暴涨说明源图不是纯双色合成，得改用别的办法）。

## 产物格式

原文件用 PNG 压缩帧（Vista+ 支持），本脚本保持同样的写法与尺寸顺序，
只换像素。不新增/删除尺寸档。
"""
import argparse
import io
import os
import struct
import sys

from PIL import Image

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ICON = os.path.join(REPO, "wind_tsf", "res", "wind_input.ico")

# 字形色：源图里的白色主字。
WHITE = (255, 255, 255)


def parse_hex(s):
    """`#RRGGBB` / `RRGGBB` → (r, g, b)。"""
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise SystemExit(f"颜色要写成 #RRGGBB：{s!r}")
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))


def read_ico(path):
    """→ [(w, h, Image)]，按目录顺序。"""
    data = open(path, "rb").read()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1:
        raise SystemExit(f"不是 ICO 文件：{path}")
    frames = []
    for i in range(count):
        w, h, _colors, _resv, _planes, _bpp, size, off = struct.unpack_from(
            "<BBBBHHII", data, 6 + i * 16
        )
        w = w or 256
        h = h or 256
        img = Image.open(io.BytesIO(data[off : off + size])).convert("RGBA")
        frames.append((w, h, img))
    return frames


def detect_plate(img):
    """取出现次数最多的不透明颜色作为底板色。"""
    from collections import Counter

    px = img.load()
    c = Counter(
        px[x, y][:3]
        for y in range(img.size[1])
        for x in range(img.size[0])
        if px[x, y][3] == 255
    )
    return c.most_common(1)[0][0]


def recolor(img, frm, to, report=False):
    """把底板色 frm 换成 to，字形色保持 WHITE，alpha 原样保留。

    `report=True` 时统计**模型自洽性**：把反解出的 c 按**原**底板色合成回去，
    与真实像素比较。误差越小说明「源图确实是 B↔W 线段混色」这个前提越成立。
    """
    px = img.load()
    w, h = img.size
    out = Image.new("RGBA", (w, h))
    op = out.load()

    d = [WHITE[i] - frm[i] for i in range(3)]
    den = sum(v * v for v in d) or 1
    errs = []

    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                op[x, y] = (0, 0, 0, 0)
                continue

            c = ((r - frm[0]) * d[0] + (g - frm[1]) * d[1] + (b - frm[2]) * d[2]) / den
            c = 0.0 if c < 0.0 else (1.0 if c > 1.0 else c)

            op[x, y] = (
                round(to[0] * (1 - c) + WHITE[0] * c),
                round(to[1] * (1 - c) + WHITE[1] * c),
                round(to[2] * (1 - c) + WHITE[2] * c),
                a,
            )

            if report and a == 255:
                errs.append(
                    max(abs((frm[i] * (1 - c) + WHITE[i] * c) - v)
                        for i, v in enumerate((r, g, b)))
                )

    if report and errs:
        errs.sort()
        print(
            f"    模型误差 n={len(errs)} 中位={errs[len(errs) // 2]:.0f} "
            f"p95={errs[int(len(errs) * 0.95)]:.0f} 最大={errs[-1]:.0f} (满量程 255)"
        )
    return out


def write_ico(path, frames):
    """按原目录格式写回：PNG 压缩帧，尺寸顺序不变。"""
    blobs = []
    for _w, _h, img in frames:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(frames))
    entries = b""
    offset = 6 + 16 * len(frames)
    for (w, h, _img), blob in zip(frames, blobs):
        entries += struct.pack(
            "<BBBBHHII",
            w if w < 256 else 0,
            h if h < 256 else 0,
            0, 0, 1, 32,
            len(blob),
            offset,
        )
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(blobs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ico", default=ICON, help="要改的 ico（默认 wind_tsf/res/wind_input.ico）")
    ap.add_argument("--to", required=True, help="新底板色 #RRGGBB")
    ap.add_argument("--from", dest="frm", default=None, help="原底板色（默认自动探测）")
    ap.add_argument("--check", action="store_true", help="只报精度，不写文件")
    ap.add_argument("--out", default=None, help="写到别处（默认原地覆盖）")
    a = ap.parse_args()

    if not os.path.isfile(a.ico):
        sys.exit(f"找不到 {a.ico}")

    frames = read_ico(a.ico)
    to = parse_hex(a.to)
    frm = parse_hex(a.frm) if a.frm else detect_plate(frames[-1][2])
    print(f"底板 {('#%02X%02X%02X' % frm)} → {('#%02X%02X%02X' % to)}，共 {len(frames)} 档")

    new = []
    for w, h, img in frames:
        print(f"  {w}x{h}")
        new.append((w, h, recolor(img, frm, to, report=a.check)))

    if a.check:
        print("（--check：未写文件）")
        return
    out = a.out or a.ico
    write_ico(out, new)
    print(f"已写入 {out}（{os.path.getsize(out)} 字节）")


if __name__ == "__main__":
    main()
