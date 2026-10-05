"""Bounded, orientation-correct startup logo processing with alpha preservation."""
from io import BytesIO
import warnings

MAX_LOGO_BYTES = 10 * 1024 * 1024
MAX_LOGO_PIXELS = 40_000_000
MAX_LOGO_DIMENSION = 16_384
LOGO_SIZE = 512


def encode_company_logo(upload):
    """Validate a raster upload and return a square transparent PNG derivative."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if getattr(upload, "size", 0) > MAX_LOGO_BYTES:
        raise ValueError("Logo image must be 10 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(upload)
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Choose a PNG, JPG or WebP image.")
            if image.width * image.height > MAX_LOGO_PIXELS or max(image.size) > MAX_LOGO_DIMENSION:
                raise ValueError("The image dimensions are too large. Choose a smaller image.")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Choose a still PNG, JPG or WebP image.")
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image.thumbnail((LOGO_SIZE, LOGO_SIZE), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0, 0))
            canvas.alpha_composite(image, ((LOGO_SIZE - image.width) // 2, (LOGO_SIZE - image.height) // 2))
            result = BytesIO()
            canvas.save(result, format="PNG", optimize=True)
            result.seek(0)
            return result
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Upload a valid PNG, JPG or WebP image with smaller dimensions.") from exc
