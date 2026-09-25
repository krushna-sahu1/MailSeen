import os
import struct
import zlib
from pathlib import Path

def create_png(width, height, get_pixel_color):
    """
    Creates an RGBA PNG from a pixel callback function get_pixel_color(x, y) -> (r, g, b, a).
    """
    raw_data = bytearray()
    for y in range(height):
        raw_data.append(0)  # Filter type None
        for x in range(width):
            r, g, b, a = get_pixel_color(x, y, width, height)
            raw_data.extend((r, g, b, a))

    def make_chunk(chunk_type, data):
        length = len(data)
        crc = zlib.crc32(chunk_type + data) & 0xffffffff
        return struct.pack(">I", length) + chunk_type + data + struct.pack(">I", crc)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = make_chunk(b"IHDR", ihdr_data)
    idat = make_chunk(b"IDAT", zlib.compress(bytes(raw_data)))
    iend = make_chunk(b"IEND", b"")

    return header + ihdr + idat + iend

def draw_icon(x, y, w, h):
    # Normalized coords from -1.0 to 1.0
    nx = (x / (w - 1)) * 2 - 1
    ny = (y / (h - 1)) * 2 - 1
    dist_sq = nx * nx + ny * ny

    # Rounded rectangle background
    corner_radius = 0.75
    if abs(nx) > corner_radius and abs(ny) > corner_radius:
        cx = 1 if nx > 0 else -1
        cy = 1 if ny > 0 else -1
        corner_dist = ((abs(nx) - corner_radius) ** 2 + (abs(ny) - corner_radius) ** 2) ** 0.5
        if corner_dist > (1 - corner_radius):
            return (0, 0, 0, 0)

    if abs(nx) > 0.92 or abs(ny) > 0.92:
        return (0, 0, 0, 0)

    # Base background: vibrant modern blue gradient
    # Top: #3b82f6 (59, 130, 246), Bottom: #1d4ed8 (29, 78, 216)
    factor = (ny + 1) / 2
    r_bg = int(59 * (1 - factor) + 29 * factor)
    g_bg = int(130 * (1 - factor) + 78 * factor)
    b_bg = int(246 * (1 - factor) + 216 * factor)

    # Draw an envelope shape
    # Envelope outer rect: x in [-0.65, 0.65], y in [-0.45, 0.45]
    if -0.62 <= nx <= 0.62 and -0.42 <= ny <= 0.42:
        # Check flap line: V shape from (-0.62, -0.42) to (0, 0.05) to (0.62, -0.42)
        target_y = -0.42 + (1.0 - abs(nx) / 0.62) * 0.47
        if abs(ny - target_y) < (2.0 / w):
            return (29, 78, 216, 255) # envelope crease
        return (255, 255, 255, 255) # white envelope body

    # Draw eye/radar badge in lower right corner
    badge_cx, badge_cy = 0.35, 0.35
    badge_dist = ((nx - badge_cx)**2 + (ny - badge_cy)**2)**0.5
    if badge_dist <= 0.38:
        if badge_dist <= 0.16:
            return (255, 255, 255, 255) # eye center
        elif badge_dist <= 0.32:
            return (16, 185, 129, 255) # emerald green tracking pulse
        else:
            return (255, 255, 255, 255) # white border

    return (r_bg, g_bg, b_bg, 255)

def main():
    icons_dir = Path("extension/icons")
    icons_dir.mkdir(parents=True, exist_ok=True)

    for size in [16, 32, 48, 128]:
        png_bytes = create_png(size, size, draw_icon)
        out_file = icons_dir / f"icon{size}.png"
        out_file.write_bytes(png_bytes)
        print(f"Generated {out_file} ({size}x{size})")

if __name__ == "__main__":
    main()
