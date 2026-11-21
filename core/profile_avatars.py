"""Validate and resize personal and team avatar uploads."""

from io import BytesIO
from secrets import token_hex
import warnings

from PIL import Image, UnidentifiedImageError


MAX_AVATAR_BYTES = 10 * 1024 * 1024
MAX_AVATAR_PIXELS = 24_000_000
MAX_AVATAR_DIMENSION = 8192
AVATAR_SIZE = (300, 300)


def encode_profile_avatar(upload):
    """Return a bounded derivative and its existing storage format."""
    if not 0 < upload.size <= MAX_AVATAR_BYTES:
        raise ValueError("Choose an image up to 10 MB.")

    output = BytesIO()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(upload) as image:
                if max(image.size) > MAX_AVATAR_DIMENSION or image.width * image.height > MAX_AVATAR_PIXELS:
                    raise ValueError("The avatar image dimensions are too large.")
                image_format = image.format or "PNG"
                image.thumbnail(AVATAR_SIZE)
                image.save(output, format=image_format)
    except (UnidentifiedImageError, OSError, KeyError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Choose an image that can be opened and resized.") from exc
    output.seek(0)
    return output, image_format.lower()


def store_profile_avatar(encoded_avatar, directory, owner_id):
    """Store a validated derivative under a new name and return its URL."""
    from .firebase_utils import upload_file_to_storage

    data, image_format = encoded_avatar
    path = f"{directory}/{owner_id}_{token_hex(8)}.{image_format}"
    return upload_file_to_storage(data, path, content_type=f"image/{image_format}")
