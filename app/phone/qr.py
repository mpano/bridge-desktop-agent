"""QR codes for pairing, drawn by macOS (Core Image), as rows of dark/light modules."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)
# PyObjC loads Core Graphics names lazily, and that isn't safe from two threads at once.
_lock = threading.Lock()


def matrix(text: str) -> list[str] | None:
    """Rows of "1" (dark) and "0", with a one-module margin; None if it can't be drawn."""
    with _lock:
        try:
            return _draw(text)
        except Exception:
            log.warning("Couldn't draw the pairing QR code.", exc_info=True)
            return None


def _draw(text: str) -> list[str] | None:
    try:
        import Quartz
        from Foundation import NSData
    except ImportError:
        return None
    data = text.encode()
    generator = Quartz.CIFilter.filterWithName_("CIQRCodeGenerator")
    generator.setValue_forKey_(NSData.dataWithBytes_length_(data, len(data)), "inputMessage")
    generator.setValue_forKey_("M", "inputCorrectionLevel")
    image = generator.outputImage()
    if image is None:
        return None
    picture = Quartz.CIContext.contextWithOptions_(None).createCGImage_fromRect_(
        image, image.extent()
    )
    width, height = Quartz.CGImageGetWidth(picture), Quartz.CGImageGetHeight(picture)
    row_bytes = Quartz.CGImageGetBytesPerRow(picture)
    step = Quartz.CGImageGetBitsPerPixel(picture) // 8
    pixels = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(picture)))
    return [
        "".join("1" if pixels[y * row_bytes + x * step] < 128 else "0" for x in range(width))
        for y in range(height)
    ]
