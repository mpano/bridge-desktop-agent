"""Render Bridge's vector monogram to crisp PNG and .icns icons (macOS only, no downloads)."""

import shutil
import subprocess
import tempfile
from pathlib import Path

# Apple's icon grid: an 824-point rounded body centred on a 1024-point canvas.
ICON_BODY = 824 / 1024


def render_png(path: Path, size: int, *, padded: bool = True) -> Path:
    import AppKit as AK

    rep = _render(size, padded)
    data = rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {})
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data.writeToFile_atomically_(str(path), True):
        raise OSError(f"Could not write {path}")
    return path


def app_icon_image(size: int = 512):
    """The Bridge icon as an NSImage, e.g. for the Dock while the window is open."""
    import AppKit as AK

    image = AK.NSImage.alloc().initWithSize_((size, size))
    image.addRepresentation_(_render(size, True))
    return image


def _render(size: int, padded: bool):
    import AppKit as AK

    from app.desktop.panel_style import draw_brand_mark

    rep = AK.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, size, size, 8, 4, True, False, AK.NSDeviceRGBColorSpace, 0, 0
    )
    context = AK.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AK.NSGraphicsContext.saveGraphicsState()
    try:
        AK.NSGraphicsContext.setCurrentContext_(context)
        context.setImageInterpolation_(AK.NSImageInterpolationHigh)
        body = size * (ICON_BODY if padded else 1.0)
        margin = (size - body) / 2
        if padded:
            shadow = AK.NSShadow.alloc().init()
            shadow.setShadowBlurRadius_(size * 0.025)
            shadow.setShadowOffset_((0, -size * 0.01))
            shadow.setShadowColor_(AK.NSColor.colorWithCalibratedWhite_alpha_(0, 0.35))
            shadow.set()
        transform = AK.NSAffineTransform.transform()
        # The mark is authored in a flipped 64×64 space.
        transform.translateXBy_yBy_(margin, size - margin)
        transform.scaleXBy_yBy_(body / 64, -body / 64)
        transform.concat()
        draw_brand_mark()
        context.flushGraphics()
    finally:
        AK.NSGraphicsContext.restoreGraphicsState()
    return rep


def render_icns(path: Path) -> Path:
    """Build a full .icns (16–1024 px) with the system iconutil."""
    iconutil = shutil.which("iconutil")
    if iconutil is None:
        raise OSError("iconutil is unavailable; install Xcode command line tools.")
    with tempfile.TemporaryDirectory() as scratch:
        iconset = Path(scratch) / "Bridge.iconset"
        for points in (16, 32, 128, 256, 512):
            render_png(iconset / f"icon_{points}x{points}.png", points)
            render_png(iconset / f"icon_{points}x{points}@2x.png", points * 2)
        subprocess.run(
            [iconutil, "--convert", "icns", "--output", str(path), str(iconset)],
            check=True,
            capture_output=True,
        )
    return path
