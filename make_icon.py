# -*- coding: utf-8 -*-
"""用 PIL 直接绘制 static/icon.svg 的等价图标，输出多尺寸 icon.ico（16/32/48/256）。
系统无 cairosvg，PIL 不能读 svg，故按 svg 的视觉手绘：
  深色圆角方底 + 琥珀金外环(粗,带缺口) + 内环(细,带缺口,错开) + 中心实心点 + 淡扫描指针。
运行：python make_icon.py   →  生成 dist/icon.ico
"""
import math
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = HERE  # icon.ico 放项目根目录，供 muliao.spec 与 installer.iss 引用
os.makedirs(OUT_DIR, exist_ok=True)

# 超采样倍数：在 viewBox(64) 基础上放大，最后缩小抗锯齿
SS = 16
VB = 64
SIZE = VB * SS  # 1024


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return (int(lerp(c1[0], c2[0], t)), int(lerp(c1[1], c2[1], t)), int(lerp(c1[2], c2[2], t)))


# svg 里的琥珀金渐变端点
AMBER_A = (0xFF, 0xC4, 0x6B)  # #ffc46b
AMBER_B = (0xFF, 0x6B, 0x3D)  # #ff6b3d
BG_HI = (0x1B, 0x22, 0x30)    # #1b2230
BG_LO = (0x08, 0x0B, 0x10)    # #080b10


def draw_gradient_arc(draw, cx, cy, r, width, start_deg, span_deg, opacity):
    """沿弧线画许多小圆点模拟带圆头、带渐变的描边（dasharray 缺口用 span<360 体现）。"""
    steps = max(2, int(span_deg * 3))  # 每 1/3 度一个点，足够平滑
    rad = width / 2.0
    for i in range(steps + 1):
        t = i / steps
        ang = math.radians(start_deg + span_deg * t)
        x = cx + r * math.cos(ang)
        y = cy + r * math.sin(ang)
        col = mix(AMBER_A, AMBER_B, t)
        col = (col[0], col[1], col[2], int(255 * opacity))
        draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=col)


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # ---- 深色圆角方底（径向渐变近似：垂直线性渐变 + 圆角遮罩）----
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bgd = ImageDraw.Draw(bg)
    for y in range(SIZE):
        t = y / SIZE
        bgd.line([(0, y), (SIZE, y)], fill=mix(BG_HI, BG_LO, t) + (255,))
    # 圆角遮罩：rx=15/64
    mask = Image.new("L", (SIZE, SIZE), 0)
    md = ImageDraw.Draw(mask)
    corner = int(15 / VB * SIZE)
    inset = int(2 / VB * SIZE)
    md.rounded_rectangle([inset, inset, SIZE - inset, SIZE - inset], radius=corner, fill=255)
    img.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(img, "RGBA")
    cx = cy = SIZE / 2.0

    # ---- 外环：r=23, width=3.4, 缺口, rotate≈-58 ----
    r_out = 23 / VB * SIZE
    w_out = 3.4 / VB * SIZE
    # 可见 ~259°，缺口 ~101°；起点角对应 svg 的 rotate(-58)
    draw_gradient_arc(draw, cx, cy, r_out, w_out, start_deg=-58, span_deg=259, opacity=0.92)

    # ---- 内环：r=14, width=2.8, 缺口, rotate≈142 ----
    r_in = 14 / VB * SIZE
    w_in = 2.8 / VB * SIZE
    draw_gradient_arc(draw, cx, cy, r_in, w_in, start_deg=142, span_deg=237, opacity=0.70)

    # ---- 扫描指针：从中心向上，淡色 ----
    ptr_col = (0xFF, 0xD9, 0xA0, int(255 * 0.45))
    draw.line([(cx, cy), (cx, cy - 20 / VB * SIZE)], fill=ptr_col, width=int(1.8 / VB * SIZE))

    # ---- 中心实心点：r=5.2, 琥珀金渐变 + 细白环 ----
    r_c = 5.2 / VB * SIZE
    # 径向渐变实心点（逐环上色）
    rings = 40
    for i in range(rings, 0, -1):
        t = i / rings
        rr = r_c * t
        col = mix(AMBER_A, AMBER_B, t)
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=col + (255,))
    # 细白环
    draw.ellipse([cx - r_c, cy - r_c, cx + r_c, cy + r_c],
                 outline=(255, 255, 255, int(255 * 0.35)), width=max(1, int(0.8 / VB * SIZE)))

    # ---- 缩小并导出多尺寸 .ico ----
    big = img.resize((256, 256), Image.LANCZOS)
    ico_path = os.path.join(OUT_DIR, "icon.ico")
    big.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    # 也存一张 256 png 方便预览
    big.save(os.path.join(OUT_DIR, "icon_256.png"), format="PNG")
    print("OK icon ->", ico_path, os.path.getsize(ico_path), "bytes")


if __name__ == "__main__":
    main()
