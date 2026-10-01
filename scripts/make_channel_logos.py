#!/usr/bin/env python3
"""Generate character-reflecting logos for channel82-94.

Each logo pairs a MOTIF evoking what the channel actually is -- a football, a basketball
seam, film-strip perforations, a peacock fan of colour -- with the channel name. The motifs
are generic genre imagery, not the broadcasters' trademarked marks: no NFL shield, no Apple
logo, no real Peacock bird. The aim is a card that reads instantly as "American football" or
"classic cinema", which is what the brand marks would have conveyed.

No PIL or ImageMagick on either host, so shapes are drawn into an RGBA framebuffer and
written through a minimal PNG encoder here; the name is composited over the result with
ffmpeg drawtext.
"""
import math, os, struct, subprocess, zlib

W, H = 220, 132
OUT = "/home/momo/stream/streaming-stack/player/logos"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TMP = "/tmp/claude-1000/-home-momo-stream/028ab9a6-b9d3-4819-a24e-ebec09ca23b2/scratchpad/motif"


# ── minimal RGBA canvas + PNG writer ─────────────────────────────────────────
class C:
    def __init__(self, w, h, bg=(0, 0, 0, 255)):
        self.w, self.h = w, h
        self.px = bytearray(bg * w * h)

    def _blend(self, x, y, col, a=1.0):
        if not (0 <= x < self.w and 0 <= y < self.h) or a <= 0:
            return
        i = (y * self.w + x) * 4
        sa = (col[3] / 255.0) * a
        for k in range(3):
            self.px[i + k] = int(self.px[i + k] * (1 - sa) + col[k] * sa)
        self.px[i + 3] = max(self.px[i + 3], int(255 * sa))

    def rect(self, x0, y0, x1, y1, col):
        for y in range(max(0, int(y0)), min(self.h, int(y1))):
            for x in range(max(0, int(x0)), min(self.w, int(x1))):
                self._blend(x, y, col)

    def vgrad(self, x0, y0, x1, y1, top, bot):
        h = max(1, y1 - y0)
        for y in range(max(0, int(y0)), min(self.h, int(y1))):
            t = (y - y0) / float(h)
            col = tuple(int(top[k] * (1 - t) + bot[k] * t) for k in range(3)) + (255,)
            for x in range(max(0, int(x0)), min(self.w, int(x1))):
                self._blend(x, y, col)

    def ellipse(self, cx, cy, rx, ry, col, rot=0.0, aa=2):
        cr, sr = math.cos(-rot), math.sin(-rot)
        for y in range(max(0, int(cy - ry - 2)), min(self.h, int(cy + ry + 3))):
            for x in range(max(0, int(cx - rx - 2)), min(self.w, int(cx + rx + 3))):
                hit = 0
                for sy in range(aa):
                    for sx in range(aa):
                        dx = x + (sx + .5) / aa - cx
                        dy = y + (sy + .5) / aa - cy
                        u, v = dx * cr - dy * sr, dx * sr + dy * cr
                        if (u / rx) ** 2 + (v / ry) ** 2 <= 1:
                            hit += 1
                if hit:
                    self._blend(x, y, col, hit / float(aa * aa))

    def ring(self, cx, cy, r, th, col, a0=0.0, a1=math.tau, aa=2, clip=None):
        for y in range(max(0, int(cy - r - th)), min(self.h, int(cy + r + th + 1))):
            for x in range(max(0, int(cx - r - th)), min(self.w, int(cx + r + th + 1))):
                hit = 0
                for sy in range(aa):
                    for sx in range(aa):
                        dx = x + (sx + .5) / aa - cx
                        dy = y + (sy + .5) / aa - cy
                        d = math.hypot(dx, dy)
                        if abs(d - r) <= th / 2.0:
                            if clip and math.hypot(x + (sx + .5) / aa - clip[0],
                                                   y + (sy + .5) / aa - clip[1]) > clip[2]:
                                continue
                            ang = math.atan2(dy, dx) % math.tau
                            if a0 <= ang <= a1 or (a1 > math.tau and ang + math.tau <= a1):
                                hit += 1
                if hit:
                    self._blend(x, y, col, hit / float(aa * aa))

    def line(self, x0, y0, x1, y1, col, th=2):
        n = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
        for i in range(n + 1):
            t = i / float(n)
            self.ellipse(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, th / 2.0, th / 2.0, col, aa=2)

    def tri(self, pts, col, aa=2):
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        def side(p, a, b):
            return (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])
        for y in range(max(0, int(min(ys))), min(self.h, int(max(ys)) + 1)):
            for x in range(max(0, int(min(xs))), min(self.w, int(max(xs)) + 1)):
                hit = 0
                for sy in range(aa):
                    for sx in range(aa):
                        p = (x + (sx + .5) / aa, y + (sy + .5) / aa)
                        d = [side(p, pts[0], pts[1]), side(p, pts[1], pts[2]), side(p, pts[2], pts[0])]
                        if all(v >= 0 for v in d) or all(v <= 0 for v in d):
                            hit += 1
                if hit:
                    self._blend(x, y, col, hit / float(aa * aa))

    def poly(self, pts, col, aa=2):
        """Scanline fill for an arbitrary (possibly concave) polygon -- a star is concave,
        and a triangle fan from one vertex renders it as a crown."""
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        n = len(pts)
        for y in range(max(0, int(min(ys))), min(self.h, int(max(ys)) + 1)):
            for x in range(max(0, int(min(xs))), min(self.w, int(max(xs)) + 1)):
                hit = 0
                for sy in range(aa):
                    for sx in range(aa):
                        px = x + (sx + .5) / aa
                        py = y + (sy + .5) / aa
                        inside = False
                        j = n - 1
                        for i in range(n):
                            yi, yj = pts[i][1], pts[j][1]
                            if (yi > py) != (yj > py):
                                xint = pts[i][0] + (py - yi) * (pts[j][0] - pts[i][0]) / (yj - yi)
                                if px < xint:
                                    inside = not inside
                            j = i
                        if inside:
                            hit += 1
                if hit:
                    self._blend(x, y, col, hit / float(aa * aa))

    def save(self, path):
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)
            raw += self.px[y * self.w * 4:(y + 1) * self.w * 4]
        def ch(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n"
               + ch(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0))
               + ch(b"IDAT", zlib.compress(bytes(raw), 9)) + ch(b"IEND", b""))
        open(path, "wb").write(png)


WHITE = (255, 255, 255, 255)
IX, IY = 46, 60          # icon centre, left of the text block


def motif(kind, bg, accent):
    c = C(W, H, bg + (255,))
    if kind == "football":
        c.ellipse(IX, IY, 30, 18, (139, 90, 43, 255), rot=math.radians(-18))
        c.line(IX - 17, IY + 6, IX + 17, IY - 6, WHITE, th=3)
        for k in (-10, -4, 2, 8):
            c.line(IX + k - 3, IY - k * 0.35 - 6, IX + k + 3, IY - k * 0.35 + 6, WHITE, th=2)
    elif kind == "basketball":
        c.ellipse(IX, IY, 27, 27, (222, 108, 36, 255))
        c.line(IX - 27, IY, IX + 27, IY, (20, 20, 20, 255), th=2)
        c.line(IX, IY - 27, IX, IY + 27, (20, 20, 20, 255), th=2)
        c.ring(IX - 26, IY, 20, 2, (20, 20, 20, 255))
        c.ring(IX + 26, IY, 20, 2, (20, 20, 20, 255))
    elif kind == "baseball":
        c.ellipse(IX, IY, 27, 27, (248, 248, 246, 255))
        for off in (-34, 34):
            c.ring(IX + off, IY, 28, 3, (200, 36, 46, 255), clip=(IX, IY, 25))
    elif kind == "puck":
        c.ellipse(IX, IY + 7, 29, 11, (18, 18, 20, 255))
        c.rect(IX - 29, IY - 6, IX + 29, IY + 8, (18, 18, 20, 255))
        c.ellipse(IX, IY - 6, 29, 11, (46, 48, 54, 255))
        c.line(IX - 34, IY + 22, IX + 34, IY + 22, accent + (255,), th=3)
    elif kind == "chevron":
        for i, o in enumerate((0, 14)):
            col = WHITE if i == 0 else accent + (255,)
            c.tri([(IX - 26 + o, IY - 22), (IX - 6 + o, IY), (IX - 26 + o, IY + 22)], col)
    elif kind == "star":
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            r = 28 if i % 2 == 0 else 12
            pts.append((IX + r * math.cos(a), IY + r * math.sin(a)))
        c.poly(pts, accent + (255,))
    elif kind == "film":
        c.rect(IX - 30, IY - 30, IX + 30, IY + 30, (28, 28, 32, 255))
        for k in range(-26, 27, 13):
            c.rect(IX - 28, IY + k - 3, IX - 20, IY + k + 3, accent + (255,))
            c.rect(IX + 20, IY + k - 3, IX + 28, IY + k + 3, accent + (255,))
        c.rect(IX - 16, IY - 26, IX + 16, IY + 26, (62, 62, 70, 255))
    elif kind == "keyhole":
        c.ellipse(IX, IY - 10, 15, 15, accent + (255,))
        c.tri([(IX - 9, IY + 2), (IX + 9, IY + 2), (IX + 5, IY + 24)], accent + (255,))
        c.tri([(IX - 9, IY + 2), (IX + 5, IY + 24), (IX - 5, IY + 24)], accent + (255,))
        c.rect(IX - 5, IY + 20, IX + 5, IY + 25, accent + (255,))
    elif kind == "plus":
        c.ring(IX, IY, 26, 3, WHITE)
        c.rect(IX - 14, IY - 3, IX + 14, IY + 3, WHITE)
        c.rect(IX - 3, IY - 14, IX + 3, IY + 14, WHITE)
    elif kind == "play":
        c.ellipse(IX, IY, 28, 28, (255, 255, 255, 40))
        c.ring(IX, IY, 27, 3, WHITE)
        c.tri([(IX - 9, IY - 15), (IX + 17, IY), (IX - 9, IY + 15)], WHITE)
    elif kind == "fan":
        cols = [(70, 160, 255), (255, 70, 110), (255, 170, 40), (90, 210, 120), (170, 110, 255)]
        for i, col in enumerate(cols):
            a = -math.pi / 2 + (i - 2) * 0.34
            c.line(IX, IY + 26, IX + 30 * math.cos(a), IY + 26 + 30 * math.sin(a), col + (255,), th=5)
            c.ellipse(IX + 30 * math.cos(a), IY + 26 + 30 * math.sin(a), 5, 5, col + (255,))
    c.rect(0, H - 6, W, H, accent + (255,))
    return c


# channel, lines, motif, background, accent
SPEC = [
    ("channel82", ["NFL", "NETWORK"],              "football",   (11, 22, 44),  (213, 10, 10)),
    ("channel83", ["NBA", "TV"],                   "basketball", (16, 20, 28),  (200, 16, 46)),
    ("channel84", ["MLB", "NETWORK"],              "baseball",   (10, 25, 51),  (200, 36, 46)),
    ("channel85", ["NHL", "NETWORK"],              "puck",       (20, 24, 31),  (110, 170, 220)),
    ("channel86", ["CBS", "SPORTS"],               "chevron",    (10, 26, 51),  (27, 122, 214)),
    ("channel87", ["STARZ", "KIDS & FAMILY"],      "star",       (20, 18, 40),  (255, 200, 40)),
    ("channel88", ["AMC+"],                        "film",       (16, 16, 16),  (212, 160, 23)),
    ("channel89", ["FX", "MOVIE"],                 "film",       (26, 14, 16),  (178, 31, 36)),
    ("channel90", ["HALLMARK", "MYSTERIES"],       "keyhole",    (24, 16, 40),  (190, 150, 60)),
    ("channel91", ["APPLE TV+", "SERIES"],         "plus",       (18, 18, 18),  (142, 142, 147)),
    ("channel92", ["HULU", "ORIGINALS"],           "play",       (11, 31, 22),  (28, 231, 131)),
    ("channel93", ["HBO MAX", "ON-DEMAND"],        "play",       (22, 16, 42),  (138, 79, 247)),
    ("channel94", ["PEACOCK", "SERIES"],           "fan",        (16, 18, 24),  (70, 160, 255)),
]


def esc(t):
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("&", "\\&")


os.makedirs(TMP, exist_ok=True)
made = []
for name, lines, kind, bg, accent in SPEC:
    mp = os.path.join(TMP, "%s.png" % name)
    if kind == "play" and name == "channel93":
        c = C(W, H)
        c.vgrad(0, 0, W, H, (34, 18, 70), (14, 12, 30))
        cc = motif(kind, bg, accent)
        for y in range(H):                       # keep the gradient, take the motif's pixels
            for x in range(W):
                i = (y * W + x) * 4
                if cc.px[i:i + 3] != bytes(bg):
                    c.px[i:i + 4] = cc.px[i:i + 4]
        c.save(mp)
    else:
        motif(kind, bg, accent).save(mp)

    tx = 84                                       # text block starts right of the icon
    avail = W - tx - 10
    n = len(lines)
    base = 34 if n == 1 else 25
    longest = max(len(x) for x in lines)
    while longest * base * 0.68 > avail and base > 10:
        base -= 1
    gap = int(base * 1.25)
    top = (H - 10 - gap * n) // 2 + 4
    f = []
    for i, ln in enumerate(lines):
        fs = base if i == 0 else max(10, int(base * 0.82))
        f.append("drawtext=fontfile=%s:text='%s':fontcolor=white:fontsize=%d:x=%d:y=%d"
                 % (FONT, esc(ln), fs, tx, top + i * gap))
    out = os.path.join(OUT, "%s.png" % name)
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp, "-vf", ",".join(f),
                        "-frames:v", "1", out], capture_output=True, text=True)
    if r.returncode != 0:
        print("  %-12s FAILED %s" % (name, (r.stderr or "").strip()[:120]))
        continue
    made.append(name)
    print("  %-12s %-22s %-11s %sb" % (name, "/".join(lines), kind, os.path.getsize(out)))

print("\ngenerated %d/%d" % (len(made), len(SPEC)))
print("DONE")
