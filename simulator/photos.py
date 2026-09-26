"""Synthetic geotagged JPEGs for testing the field-photo import.

Each file is a drawing (not a photo) with the same botanical cues as the robot
frames. Its EXIF ImageDescription contains "SYNTHETIC TEST PHOTO", which makes the
importer mark the mission as simulated, so these files can never pass as real
field data. The set deliberately includes the awkward cases real folders
contain: missing accuracy, missing time zone, no GPS, a photo outside Bonn, a
duplicate copy and a non-image file.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ExifTags
from PIL.TiffImagePlugin import IFDRational

from forestcare.photos import SYNTHETIC_MARKER

W, H = 1200, 900

_LEAF = {  # length, width, colour, glossy, serrated
    "Prunus serotina": (150, 36, (47, 93, 42), True, True),
    "Prunus padus": (140, 52, (108, 154, 74), False, True),
    "Frangula alnus": (115, 58, (91, 138, 60), False, False),
}


@dataclass
class SamplePhoto:
    filename: str
    taxon: str | None                 # None for files that are not plant photos
    lat: float | None
    lon: float | None
    local_time: str = "2026:09:20 10:30:00"
    offset: str | None = "+02:00"
    accuracy_m: float | None = 4.7
    heading_deg: float | None = 210.0
    camera: tuple[str, str] = ("Apple", "iPhone 13")
    stand_key: str | None = None      # ground-truth link for the demo/tests
    height: str = "shrub"
    synthetic_marker: bool = True     # tests switch this off to exercise the real-photo path


def _rational(x: float, den: int = 10000) -> IFDRational:
    return IFDRational(round(x * den), den)


def _dms(value: float) -> tuple:
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m / 60) * 3600
    return (IFDRational(d, 1), IFDRational(m, 1), _rational(s, 1000))


def _leaf(draw: ImageDraw.ImageDraw, x: float, y: float, angle: float, spec: tuple) -> None:
    length, width, colour, glossy, serrated = spec
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))

    def at(lx: float, ly: float) -> tuple[float, float]:
        return x + lx * ca - ly * sa, y + lx * sa + ly * ca

    steps = [i / 20 for i in range(21)]
    upper = [at(length * t, width / 2 * math.sin(math.pi * t)) for t in steps]
    lower = [at(length * t, -width / 2 * math.sin(math.pi * t)) for t in reversed(steps)]
    draw.polygon(upper + lower, fill=colour, outline=(25, 45, 20))
    if serrated:
        for px, py in (upper + lower)[1::2]:
            draw.ellipse([px - 2, py - 2, px + 2, py + 2], fill=(25, 45, 20))
    if glossy:
        draw.line([at(length * t, width * 0.18) for t in (0.2, 0.5, 0.8)], fill=(235, 245, 235), width=6)


def render(taxon: str, seed: int, caption: str) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (W, H), (120, 140, 90))
    draw = ImageDraw.Draw(img)
    for y in range(H):  # sky-to-ground gradient
        c = int(170 - 90 * y / H)
        draw.line([(0, y), (W, y)], fill=(c - 20, c, c - 50))
    for _ in range(40):
        r = rng.randint(30, 110)
        cx, cy = rng.randint(0, W), rng.randint(0, H)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(44, 68, 36))
    img = img.filter(ImageFilter.GaussianBlur(12))
    draw = ImageDraw.Draw(img)
    draw.line([(120, 780), (500, 560), (1080, 260)], fill=(90, 65, 39), width=18)
    spec = _LEAF[taxon]
    for i in range(9):
        t = (i + 1) / 10
        bx, by = 120 + 960 * t, 780 - 520 * t
        _leaf(draw, bx, by, (-120 if i % 2 else 10) + rng.uniform(-20, 20), spec)
    if taxon == "Prunus serotina":  # drooping raceme with black cherries
        for i in range(11):
            cx, cy = 620 + 18 * math.sin(i / 2), 520 + i * 26
            draw.ellipse([cx - 13, cy - 13, cx + 13, cy + 13], fill=(26, 15, 20), outline=(0, 0, 0))
            draw.ellipse([cx - 4, cy - 17, cx + 4, cy - 9], fill=(122, 143, 58))
    elif taxon == "Frangula alnus":  # berries in the leaf axils
        for _ in range(12):
            t = rng.uniform(0.2, 0.9)
            cx, cy = 120 + 960 * t + rng.uniform(-15, 15), 780 - 520 * t + rng.uniform(-5, 25)
            draw.ellipse([cx - 12, cy - 12, cx + 12, cy + 12], fill=rng.choice([(179, 38, 30), (26, 15, 20), (107, 143, 58)]))
    font = ImageFont.load_default(size=64)
    small = ImageFont.load_default(size=28)
    draw.text((W / 2, H / 2), SYNTHETIC_MARKER, font=font, fill=(255, 255, 255), anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))
    draw.rectangle([0, H - 54, W, H], fill=(0, 0, 0))
    draw.text((20, H - 42), caption, font=small, fill=(255, 255, 255))
    return img


def jpeg_bytes(p: SamplePhoto, seed: int = 0) -> bytes:
    img = render(p.taxon, seed, f"{p.filename} · synthetic drawing for testing the import, not a photo")
    exif = Image.Exif()
    exif[ExifTags.Base.Make], exif[ExifTags.Base.Model] = p.camera
    exif[ExifTags.Base.ImageDescription] = (f"{SYNTHETIC_MARKER} generated by forestcare simulator/photos.py"
                                             if p.synthetic_marker else "field photo")
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[ExifTags.Base.DateTimeOriginal] = p.local_time
    if p.offset:
        sub[ExifTags.Base.OffsetTimeOriginal] = p.offset
    if p.lat is not None and p.lon is not None:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[ExifTags.GPS.GPSLatitudeRef] = "N" if p.lat >= 0 else "S"
        gps[ExifTags.GPS.GPSLatitude] = _dms(p.lat)
        gps[ExifTags.GPS.GPSLongitudeRef] = "E" if p.lon >= 0 else "W"
        gps[ExifTags.GPS.GPSLongitude] = _dms(p.lon)
        gps[ExifTags.GPS.GPSAltitudeRef] = b"\x00"
        gps[ExifTags.GPS.GPSAltitude] = _rational(92.4, 10)
        if p.accuracy_m is not None:
            gps[ExifTags.GPS.GPSHPositioningError] = _rational(p.accuracy_m, 100)
        if p.heading_deg is not None:
            gps[ExifTags.GPS.GPSImgDirectionRef] = "T"
            gps[ExifTags.GPS.GPSImgDirection] = _rational(p.heading_deg, 100)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, exif=exif)
    return buf.getvalue()


def demo_set(world) -> list[SamplePhoto]:
    """A field walk on 20 Sept 2026 touching several simulated stands plus the usual problem files."""
    at = {s.spec.key: (s.lat, s.lon) for s in world.stands}

    def near(key: str, east_m: float, north_m: float) -> tuple[float, float]:
        lat, lon = at[key]
        return lat + north_m / 111_200, lon + east_m / 70_400

    return [
        SamplePhoto("walk/IMG_4101.JPG", "Prunus serotina", *near("TB-1", 2, -1), "2026:09:20 10:12:05", stand_key="TB-1"),
        SamplePhoto("walk/IMG_4102.JPG", "Prunus serotina", *near("TB-1", -3, 2), "2026:09:20 10:14:40", stand_key="TB-1"),
        SamplePhoto("walk/IMG_4117.JPG", "Prunus serotina", *near("RA-1", 1, 1), "2026:09:20 11:02:11",
                    accuracy_m=None, stand_key="RA-1", height="tree"),
        SamplePhoto("walk/IMG_4130.JPG", "Frangula alnus", *near("EN-F", 0, 3), "2026:09:20 11:40:52", stand_key="EN-F"),
        # Melbtal (NSG BN-011, Venusberg): a place the robot never surveys
        SamplePhoto("walk/IMG_4152.JPG", "Prunus serotina", 50.70819, 7.08600, "2026:09:20 12:31:09",
                    accuracy_m=6.2, stand_key="PH-MELB"),
        # Camera without time zone information: local time is assumed to be Bonn time
        SamplePhoto("walk/DSC_0077.JPG", "Prunus serotina", *near("KF-1", 1, 0), "2026:09:20 13:05:44",
                    offset=None, accuracy_m=None, camera=("NIKON CORPORATION", "COOLPIX W300"), stand_key="KF-1"),
        SamplePhoto("walk/IMG_4160.JPG", "Prunus serotina", None, None, "2026:09:20 13:20:00", stand_key=None),
        SamplePhoto("walk/IMG_4188.JPG", "Prunus serotina", 50.69000, 7.00000, "2026:09:20 14:02:30", stand_key=None),
    ]


def write_folder(folder: Path, photos: list[SamplePhoto]) -> dict[str, dict]:
    """Write the JPEGs (+ a duplicate copy and a notes file). Returns ground truth keyed by file name."""
    folder.mkdir(parents=True, exist_ok=True)
    truth = {}
    for i, p in enumerate(photos):
        target = folder / p.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(jpeg_bytes(p, seed=i))
        truth[p.filename] = {"taxon": p.taxon, "stand_key": p.stand_key or "photo-other", "height": p.height}
    first = folder / photos[0].filename
    dup = first.with_name(first.stem + " (1)" + first.suffix)
    dup.write_bytes(first.read_bytes())
    truth[dup.relative_to(folder).as_posix()] = truth[photos[0].filename]
    (folder / "walk" / "notes.txt").write_text("Field walk 2026-09-20, synthetic test data.\n")
    return truth
