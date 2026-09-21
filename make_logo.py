"""Generate the PC Control app icon (icon.png)."""
from PIL import Image, ImageDraw
import math

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded-square dark background
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=110, fill=(13, 17, 28, 255),
                    outline=(40, 60, 90, 255), width=6)

cx = cy = S // 2
outer, inner = 190, 60

# Fan blades: 5 curved petals, cyan -> blue
def blade(angle_deg, color):
    a = math.radians(angle_deg)
    pts = []
    for i in range(41):
        t = i / 40.0
        r = inner + (outer - inner) * t
        sweep = math.radians(70) * t  # curl the blade
        aa = a + sweep
        w = 8 + 55 * math.sin(math.pi * min(t * 1.15, 1.0))
        pts.append((cx + r * math.cos(aa) + w * math.cos(aa + math.pi / 2),
                    cy + r * math.sin(aa) + w * math.sin(aa + math.pi / 2)))
    for i in range(40, -1, -1):
        t = i / 40.0
        r = inner + (outer - inner) * t
        sweep = math.radians(70) * t
        aa = a + sweep
        w = 8 + 55 * math.sin(math.pi * min(t * 1.15, 1.0))
        pts.append((cx + r * math.cos(aa) - w * math.cos(aa + math.pi / 2),
                    cy + r * math.sin(aa) - w * math.sin(aa + math.pi / 2)))
    d.polygon(pts, fill=color)

blues = [(56, 189, 248), (34, 150, 240), (28, 120, 235), (60, 180, 250), (30, 140, 238)]
for i in range(5):
    blade(i * 72 - 90, blues[i] + (235,))

# Hub
d.ellipse([cx - 58, cy - 58, cx + 58, cy + 58], fill=(8, 12, 22, 255),
          outline=(56, 189, 248, 255), width=6)
d.ellipse([cx - 26, cy - 26, cx + 26, cy + 26], fill=(56, 189, 248, 255))

# Gauge arc + needle in the hub area accent (bottom-right spark)
arc_r = 215
d.arc([cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r],
      start=300, end=60, fill=(250, 140, 40, 255), width=14)
ax, ay = cx + arc_r * math.cos(math.radians(60)), cy - arc_r * math.sin(math.radians(60))
d.ellipse([ax - 16, ay - 16, ax + 16, ay + 16], fill=(250, 140, 40, 255))

img.save("icon.png")
img.resize((64, 64), Image.LANCZOS).save("icon_small.png")
print("saved icon.png / icon_small.png")
