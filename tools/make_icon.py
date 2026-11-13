"""Gera icone.ico (bandeira quadriculada) sem dependências: python tools/make_icon.py"""
import math
import os
import struct
import zlib

S = 1024  # desenha em alta resolução e reduz (antialiasing)
K = S / 256


def color_at(x, y):
    """Cor RGBA no espaço 256x256 (coordenadas em float)."""
    # mastro
    if 38 <= x <= 52 and 30 <= y <= 240:
        sh = 0.75 + 0.25 * math.cos((x - 45) / 7 * 1.4)
        return (int(190 * sh), int(196 * sh), int(205 * sh), 255)
    if (x - 45) ** 2 + (y - 26) ** 2 <= 11 ** 2:
        return (230, 190, 70, 255)
    # bandeira ondulada
    u = (x - 52) / 186
    if 0 <= u <= 1:
        top = 36 + 13 * math.sin(2 * math.pi * u * 0.95) * (0.3 + u)
        h = 132 - 18 * u
        v = (y - top) / h
        if 0 <= v <= 1:
            edge = min(x - 52, 238 - x, y - top, top + h - y)
            if edge < 5:
                return (215, 220, 228, 255)
            col, row = int(u * 6), int(v * 4)
            shade = 0.80 + 0.20 * math.cos(2 * math.pi * u * 0.95)
            if (col + row) % 2 == 0:
                c = int(250 * shade)
                return (c, c, c, 255)
            c = int(30 * shade)
            return (c, c, c + 4, 255)
    return (0, 0, 0, 0)


def render():
    img = [[color_at((i + 0.5) / K, (j + 0.5) / K) for i in range(S)] for j in range(S)]
    return img


def downsample(img, size):
    f = S // size
    out = []
    for j in range(size):
        row = []
        for i in range(size):
            r = g = b = a = 0
            for dj in range(f):
                line = img[j * f + dj]
                for di in range(f):
                    p = line[i * f + di]
                    r += p[0] * p[3]; g += p[1] * p[3]; b += p[2] * p[3]; a += p[3]
            n = f * f
            row.append((r // a, g // a, b // a, a // n) if a else (0, 0, 0, 0))
        out.append(row)
    return out


def png(pix):
    h, w = len(pix), len(pix[0])
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in pix)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    big = render()
    sizes = [256, 64, 48, 32, 16]
    imgs = [png(downsample(big, s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    entries, data = b"", b""
    for s, d in zip(sizes, imgs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(d), off + len(data))
        data += d
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "icone.ico"), "wb") as fh:
        fh.write(head + entries + data)
    with open(os.path.join(here, "web", "icone.png"), "wb") as fh:
        fh.write(imgs[1])
    print("icone.ico gerado")


if __name__ == "__main__":
    main()
