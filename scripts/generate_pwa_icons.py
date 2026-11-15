"""Generate high-DPI anti-aliased PWA & iOS icons for Rove (Unified R Ribbon emblem)."""
import math
from pathlib import Path
import struct
import zlib

PUBLIC_DIR = Path(__file__).resolve().parents[1] / 'frontend/public'


def _clamp01(v: float) -> float:
    return 0.0 if v <= 0.0 else (1.0 if v >= 1.0 else v)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _dist_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0.0:
        return math.hypot(px - ax, py - ay)
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return math.hypot(px - bx, py - by)
    t = c1 / c2
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _dist_to_right_semicircle(px: float, py: float, cx: float, cy: float, radius: float) -> float:
    dx = px - cx
    dy = py - cy
    if dx >= 0.0:
        return abs(math.hypot(dx, dy) - radius)
    return min(math.hypot(dx, py - (cy - radius)), math.hypot(dx, py - (cy + radius)))


def _rounded_rect_sdf(px: float, py: float, cx: float, cy: float, half_w: float, half_h: float, radius: float) -> float:
    qx = abs(px - cx) - (half_w - radius)
    qy = abs(py - cy) - (half_h - radius)
    out_dist = math.hypot(max(qx, 0.0), max(qy, 0.0))
    in_dist = min(max(qx, qy), 0.0)
    return out_dist + in_dist - radius


def _write_png(path: Path, width: int, height: int, rgba_bytes: bytes, opaque_rgb: bool = False) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', crc)

    color_type = 2 if opaque_rgb else 6
    ihdr = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)
        row = rgba_bytes[y * stride:(y + 1) * stride]
        if opaque_rgb:
            for i in range(0, len(row), 4):
                rows.extend(row[i:i + 3])
        else:
            rows.extend(row)

    compressed = zlib.compress(bytes(rows), level=9)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')
    path.write_bytes(png)


def render_icon(size: int, *, rounded: bool, glyph_scale: float, opaque_rgb: bool = False) -> bytes:
    """Render the unified Rove R ribbon icon using analytic SDF anti-aliasing."""
    aa = 1.15 / size
    stroke_r = 0.054 * glyph_scale

    def glyph_coords(nx: float, ny: float):
        return (nx - 0.5) / glyph_scale + 0.5, (ny - 0.5) / glyph_scale + 0.5

    buf = bytearray(size * size * 4)
    idx = 0
    for y in range(size):
        ny = (y + 0.5) / size
        for x in range(size):
            nx = (x + 0.5) / size

            if rounded:
                sdf_card = _rounded_rect_sdf(nx, ny, 0.5, 0.5, 0.468, 0.468, 0.215)
                card_alpha = _clamp01(0.5 - sdf_card / aa)
            else:
                sdf_card = -1.0
                card_alpha = 1.0

            if card_alpha <= 0.0:
                buf[idx:idx + 4] = b'\x00\x00\x00\x00'
                idx += 4
                continue

            diag = _clamp01(nx * 0.40 + ny * 0.75)
            ambient = math.exp(-((nx - 0.50) ** 2 + (ny - 0.48) ** 2) / 0.22)
            bg_r = _lerp(18.0, 11.0, diag) + 12.0 * ambient
            bg_g = _lerp(24.0, 14.0, diag) + 18.0 * ambient
            bg_b = _lerp(38.0, 22.0, diag) + 32.0 * ambient

            if rounded:
                border_band = 1.0 - _clamp01(abs(sdf_card + 0.010) / (0.011 + aa))
                highlight = border_band * _lerp(0.32, 0.07, ny)
                bg_r = _lerp(bg_r, 148.0, highlight)
                bg_g = _lerp(bg_g, 184.0, highlight)
                bg_b = _lerp(bg_b, 245.0, highlight)

            gx, gy = glyph_coords(nx, ny)

            # R geometry:
            # Stem: x = 0.28, y in [0.22, 0.78]
            # Top bar: (0.28, 0.22) to (0.45, 0.22)
            # Arc: center (0.45, 0.36), radius 0.14
            # Mid bar: (0.28, 0.50) to (0.45, 0.50)
            # Bold leg: (0.43, 0.50) to (0.74, 0.78)
            d_stem = _dist_to_segment(gx, gy, 0.28, 0.22, 0.28, 0.78)
            d_top = _dist_to_segment(gx, gy, 0.28, 0.22, 0.45, 0.22)
            d_arc = _dist_to_right_semicircle(gx, gy, 0.45, 0.36, 0.14)
            d_mid = _dist_to_segment(gx, gy, 0.28, 0.50, 0.45, 0.50)
            d_leg = _dist_to_segment(gx, gy, 0.43, 0.50, 0.74, 0.78)

            d_glyph = min(d_stem, d_top, d_arc, d_mid, d_leg) * glyph_scale - stroke_r
            glyph_alpha = _clamp01(0.5 - d_glyph / aa)

            r, g, b = bg_r, bg_g, bg_b

            # Drop shadow beneath glyph
            if glyph_alpha < 1.0:
                sgx, sgy = glyph_coords(nx, ny - 0.018)
                sd = min(
                    _dist_to_segment(sgx, sgy, 0.28, 0.22, 0.28, 0.78),
                    _dist_to_segment(sgx, sgy, 0.28, 0.22, 0.45, 0.22),
                    _dist_to_right_semicircle(sgx, sgy, 0.45, 0.36, 0.14),
                    _dist_to_segment(sgx, sgy, 0.28, 0.50, 0.45, 0.50),
                    _dist_to_segment(sgx, sgy, 0.43, 0.50, 0.74, 0.78),
                ) * glyph_scale - stroke_r * 1.15
                sh_alpha = _clamp01(0.5 - sd / (aa * 4.0)) * 0.40
                r *= (1.0 - sh_alpha)
                g *= (1.0 - sh_alpha)
                b *= (1.0 - sh_alpha)

            # Elegant color flow: porcelain white top -> luminous sky cyan -> electric indigo -> violet
            if glyph_alpha > 0.0:
                t_color = _clamp01((gy - 0.24) / 0.53 + (gx - 0.28) * 0.42)
                if t_color < 0.35:
                    k = t_color / 0.35
                    gr = _lerp(255.0, 240.0, k)
                    gg = _lerp(255.0, 249.0, k)
                    gb = _lerp(255.0, 255.0, k)
                elif t_color < 0.70:
                    k = (t_color - 0.35) / 0.35
                    gr = _lerp(240.0, 56.0, k)
                    gg = _lerp(249.0, 189.0, k)
                    gb = _lerp(255.0, 248.0, k)
                else:
                    k = (t_color - 0.70) / 0.30
                    gr = _lerp(56.0, 129.0, k)
                    gg = _lerp(189.0, 140.0, k)
                    gb = _lerp(248.0, 248.0, k)

                r = _lerp(r, gr, glyph_alpha)
                g = _lerp(g, gg, glyph_alpha)
                b = _lerp(b, gb, glyph_alpha)

            buf[idx] = max(0, min(255, int(round(r))))
            buf[idx + 1] = max(0, min(255, int(round(g))))
            buf[idx + 2] = max(0, min(255, int(round(b))))
            buf[idx + 3] = 255 if opaque_rgb else max(0, min(255, int(round(card_alpha * 255.0))))
            idx += 4

    return bytes(buf)


def main() -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    # Standard names AND versioned names to completely defeat any stale cache
    png_192 = render_icon(192, rounded=True, glyph_scale=0.82)
    png_512 = render_icon(512, rounded=True, glyph_scale=0.82)
    png_maskable = render_icon(512, rounded=False, glyph_scale=0.66),
    png_apple = render_icon(180, rounded=False, glyph_scale=0.78),

    _write_png(PUBLIC_DIR / 'pwa-192.png', 192, 192, png_192)
    _write_png(PUBLIC_DIR / 'rove-192.png', 192, 192, png_192)

    _write_png(PUBLIC_DIR / 'pwa-512.png', 512, 512, png_512)
    _write_png(PUBLIC_DIR / 'rove-512.png', 512, 512, png_512)

    _write_png(PUBLIC_DIR / 'pwa-maskable-512.png', 512, 512, png_maskable[0], opaque_rgb=True)
    _write_png(PUBLIC_DIR / 'rove-maskable-512.png', 512, 512, png_maskable[0], opaque_rgb=True)

    _write_png(PUBLIC_DIR / 'apple-touch-icon.png', 180, 180, png_apple[0], opaque_rgb=True)
    _write_png(PUBLIC_DIR / 'rove-apple-touch.png', 180, 180, png_apple[0], opaque_rgb=True)

    # Matching crisp SVG favicon for Rove with gradient
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<defs>'
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#172034"/><stop offset="100%" stop-color="#0b0f19"/>'
        '</linearGradient>'
        '<linearGradient id="rgrad" x1="18" y1="14" x2="48" y2="50" gradientUnits="userSpaceOnUse">'
        '<stop offset="0%" stop-color="#ffffff"/>'
        '<stop offset="42%" stop-color="#f0f9ff"/>'
        '<stop offset="72%" stop-color="#38bdf8"/>'
        '<stop offset="100%" stop-color="#818cf8"/>'
        '</linearGradient>'
        '</defs>'
        '<rect x="2" y="2" width="60" height="60" rx="15" fill="url(#bg)" stroke="#334155" stroke-width="1.5"/>'
        '<path d="M19.5 50V15H33.8a8.8 8.8 0 0 1 0 17.6H19.5" fill="none" stroke="url(#rgrad)" stroke-width="6.6" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M31.5 32.6 45.5 50" fill="none" stroke="url(#rgrad)" stroke-width="6.6" stroke-linecap="round"/>'
        '</svg>'
    )
    (PUBLIC_DIR / 'favicon.svg').write_text(svg, encoding='utf-8')
    (PUBLIC_DIR / 'rove-favicon.svg').write_text(svg, encoding='utf-8')
    print('Generated Rove PWA icons in', PUBLIC_DIR)


if __name__ == '__main__':
    main()
