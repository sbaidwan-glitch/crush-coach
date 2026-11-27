"""Photos on a notebook entry: what the server accepts, and what it keeps. Pure; no database.

The browser has already resized each photo to about 1,600 px and under 1.5 MB (`notebook.js`),
but a browser is not to be trusted: whatever arrives is opened here as an image, and the file
the app stores is a fresh JPEG made from the decoded pixels. That does three things at once:

- **The real type is checked.** A file's name and its `Content-Type` are claims; decoding it
  is the proof. Only JPEG, PNG and WebP pictures pass (what a phone camera and a screenshot
  give); anything else, a renamed script or a PDF included, is refused.
- **Location data is stripped.** Nothing from the original is copied, so GPS position, the
  camera, the time and the rest of the EXIF block are gone. The photo is turned upright first,
  using its orientation tag, so it still looks right.
- **Size is capped** at `MAX_BYTES` and `MAX_SIDE` even if the browser skipped its resize.
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PHOTOS = 6  # per entry
MAX_BYTES = 1_500_000  # 1.5 MB, per photo, what is sent and what is stored
MAX_SIDE = 1600  # pixels, the longer side
MAX_PIXELS = 50_000_000  # refuse a "decompression bomb": a small file that unpacks huge
ACCEPTED_FORMATS = ("JPEG", "PNG", "MPO", "WEBP")  # MPO is how some phones write a JPEG
CONTENT_TYPE = "image/jpeg"
_QUALITIES = (85, 78, 70, 62, 54)

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class PhotoError(ValueError):
    """A photo that was not kept. The message is safe to show a member."""


@dataclass(frozen=True)
class Cleaned:
    data: bytes  # a JPEG with no metadata
    width: int
    height: int


def _flatten(image: Image.Image) -> Image.Image:
    """RGB, with transparency laid on white (a PNG may have some; JPEG has none)."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return image.convert("RGB")


def clean(raw: bytes, name: str = "") -> Cleaned:
    """The stored version of an uploaded photo. Raises PhotoError (safe to show) when the
    file is empty, too big, not a picture of an accepted kind, or can't be read."""
    label = f"“{name}” " if name else "That file "
    if not raw:
        raise PhotoError(f"{label}is empty.")
    if len(raw) > MAX_BYTES:
        raise PhotoError(
            f"{label}is over {MAX_BYTES / 1_000_000:.1f} MB. "
            "Photos are shrunk in the browser; try a smaller one."
        )
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in ACCEPTED_FORMATS:
                raise PhotoError(f"{label}isn’t a photo. Use a JPEG, PNG or WebP picture.")
            probe.verify()  # catches a truncated or damaged file before it is decoded
        with Image.open(io.BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened)  # upright, using the tag we then drop
            image.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise PhotoError(f"{label}isn’t a photo this app can read.") from None
    image = _flatten(image)
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)

    for quality in _QUALITIES:
        buffer = io.BytesIO()
        # No `exif`, `icc_profile` or `info` is passed, so none of it is written.
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= MAX_BYTES:
            return Cleaned(buffer.getvalue(), image.width, image.height)
    raise PhotoError(f"{label}is too detailed to fit in {MAX_BYTES / 1_000_000:.1f} MB.")
