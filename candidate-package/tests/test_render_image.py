"""Tests for rfqparse.render.image with images built here by Pillow.

    python tests/test_render_image.py -v
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rfqparse import config  # noqa: E402
from rfqparse.render import image as I  # noqa: E402


def encode(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    b = io.BytesIO()
    img.save(b, format=fmt, **kw)
    return b.getvalue()


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def screenshot(w=300, h=200) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for y in range(10, h, 24):
        d.text((8, y), "6 EA  2in 90 elbow  A234", fill="black")
    return img


def photo_like(w=400, h=300) -> Image.Image:
    return Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).resize((w, h))


class PrepareTests(unittest.TestCase):
    def test_plain_png_passes_through_with_sizes(self):
        data = encode(screenshot())
        r = I.prepare_image(data)
        self.assertTrue(r.ok)
        self.assertEqual((r.width, r.height, r.orig_width, r.orig_height), (300, 200, 300, 200))
        self.assertEqual(r.orig_bytes, len(data))
        self.assertEqual(opened(r.data).size, (300, 200))
        self.assertEqual(opened(r.data).mode, "RGB")
        self.assertFalse(r.rotated or r.downscaled)

    def test_exif_rotation_applied(self):
        img = Image.new("RGB", (300, 200), "white")
        ImageDraw.Draw(img).rectangle((0, 0, 40, 40), fill="red")        # marker in the top-left corner
        exif = Image.Exif()
        exif[0x0112] = 6                                                  # "rotate 90 degrees clockwise to display"
        r = I.prepare_image(encode(img, "JPEG", exif=exif))
        self.assertTrue(r.ok)
        self.assertTrue(r.rotated)
        self.assertEqual((r.orig_width, r.orig_height), (300, 200))
        self.assertEqual((r.width, r.height), (200, 300))                 # now portrait
        out = opened(r.data).convert("RGB")
        red, white = out.getpixel((180, 20)), out.getpixel((20, 20))      # marker moved to the top-right
        self.assertGreater(red[0], 200)
        self.assertLess(red[1], 80)
        self.assertGreater(white[1], 200)
        self.assertIn("rotated", r.text)

    def test_no_exif_orientation_is_not_reported_as_rotated(self):
        self.assertFalse(I.prepare_image(encode(photo_like(), "JPEG")).rotated)

    def test_downscaled_to_configured_long_edge_keeping_aspect(self):
        r = I.prepare_image(encode(Image.new("RGB", (3000, 1000), "white")))
        self.assertEqual((r.width, r.height), (2000, 667))
        self.assertTrue(r.downscaled)
        self.assertEqual((r.orig_width, r.orig_height), (3000, 1000))
        with mock.patch.object(config, "IMAGE_MAX_EDGE", 100):
            r2 = I.prepare_image(encode(Image.new("RGB", (400, 200), "white")))
        self.assertEqual((r2.width, r2.height), (100, 50))
        self.assertEqual(config.IMAGE_MAX_EDGE, 2000)

    def test_small_images_are_not_upscaled(self):
        r = I.prepare_image(encode(Image.new("RGB", (120, 90), "white")))
        self.assertEqual((r.width, r.height), (120, 90))
        self.assertFalse(r.downscaled)

    def test_transparency_goes_onto_white_not_black(self):
        img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle((25, 0, 49, 49), fill=(255, 0, 0, 255))
        out = opened(I.prepare_image(encode(img)).data).convert("RGB")
        self.assertEqual(out.getpixel((5, 5)), (255, 255, 255))
        self.assertEqual(out.getpixel((40, 5)), (255, 0, 0))

    def test_palette_with_transparency_and_other_modes_become_rgb(self):
        pal = Image.new("RGBA", (40, 40), (0, 0, 0, 0)).convert("P")
        pal.info["transparency"] = 0
        cases = {"palette+transparency": pal, "grayscale": Image.new("L", (40, 40), 128),
                 "1-bit": Image.new("1", (40, 40), 1), "CMYK": Image.new("CMYK", (40, 40), (0, 0, 0, 0)),
                 "16-bit gray": Image.new("I;16", (40, 40), 40000)}
        for name, img in cases.items():
            with self.subTest(name):
                fmt = "JPEG" if name == "CMYK" else "PNG"
                r = I.prepare_image(encode(img, fmt))
                self.assertTrue(r.ok, r.reason)
                self.assertEqual(opened(r.data).mode, "RGB")

    def test_png_for_screenshots_jpeg_for_photos(self):
        self.assertEqual(I.prepare_image(encode(screenshot())).mime, "image/png")
        self.assertEqual(I.prepare_image(encode(photo_like(), "PNG")).mime, "image/jpeg")

    def test_multi_frame_uses_first_frame(self):
        frames = [Image.new("RGB", (60, 40), c) for c in ("red", "blue")]
        r = I.prepare_image(encode(frames[0], "GIF", save_all=True, append_images=frames[1:]))
        self.assertTrue(r.ok)
        self.assertEqual((r.width, r.height), (60, 40))


class SkipTests(unittest.TestCase):
    def test_tracking_pixel_skipped_with_a_reasoned_note(self):
        r = I.prepare_image(encode(Image.new("RGB", (1, 1), "white"), "GIF"))
        self.assertTrue(r.skipped)
        self.assertFalse(r.ok)
        self.assertEqual(r.data, b"")
        self.assertIn("IMAGE SKIPPED", r.text)
        self.assertIn("1x1", r.text)
        self.assertIn("tracking pixel", r.text)
        self.assertEqual((r.orig_width, r.orig_height), (1, 1))

    def test_thin_spacer_skipped_but_small_real_image_kept(self):
        self.assertTrue(I.prepare_image(encode(Image.new("RGB", (800, 3), "white"))).skipped)
        self.assertTrue(I.prepare_image(encode(Image.new("RGB", (config.IMAGE_MIN_EDGE - 1, 200), "white"))).skipped)
        self.assertTrue(I.prepare_image(encode(Image.new("RGB", (config.IMAGE_MIN_EDGE, config.IMAGE_MIN_EDGE), "white"))).ok)

    def test_min_edge_is_configurable(self):
        data = encode(Image.new("RGB", (40, 40), "white"))
        self.assertTrue(I.prepare_image(data).ok)
        with mock.patch.object(config, "IMAGE_MIN_EDGE", 64):
            self.assertTrue(I.prepare_image(data).skipped)

    def test_no_filename_or_logo_logic_exists(self):
        import inspect
        self.assertEqual(list(inspect.signature(I.prepare_image).parameters), ["data"])   # bytes only, nothing to key on


class UnreadableTests(unittest.TestCase):
    def test_never_raises_and_gives_a_reason(self):
        good = encode(screenshot())
        jpeg = encode(photo_like(), "JPEG")
        cases = {
            "empty": (b"", "empty"),
            "garbage": (b"this is not an image at all", "unsupported or corrupt"),
            "png header then garbage": (good[:30] + b"\x00garbage" * 20, "corrupt|unsupported"),
            "truncated png": (good[: len(good) // 2], "truncated|corrupt"),
            "truncated jpeg": (jpeg[: len(jpeg) // 2], "truncated|corrupt"),
            "heic": (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64, "HEIC"),
            "avif": (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1" + b"\x00" * 64, "AVIF"),
            "svg": (b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>', "SVG"),
            "pdf": (b"%PDF-1.4\n%....", "PDF"),
        }
        for name, (blob, pattern) in cases.items():
            with self.subTest(name):
                r = I.prepare_image(blob)
                self.assertEqual(r.status, "unreadable")
                self.assertFalse(r.ok)
                self.assertRegex(r.reason, pattern)
                self.assertEqual(r.data, b"")

    def test_pixel_limit_and_size_limit(self):
        data = encode(Image.new("RGB", (100, 100), "white"))
        with mock.patch.object(config, "IMAGE_MAX_PIXELS", 1000):
            self.assertIn("too large", I.prepare_image(data).reason)
        with mock.patch.object(I, "MAX_BYTES", 10):
            self.assertIn("too large", I.prepare_image(data).reason)

    def test_non_bytes_input_does_not_raise(self):
        self.assertEqual(I.prepare_image(None).status, "unreadable")


class RealFileSmokeTest(unittest.TestCase):
    """Every image attachment in the dev inbox prepares cleanly (real files, not built here)."""

    EMAILS = Path(__file__).resolve().parent.parent / "data" / "dev" / "emails"

    def test_all_dev_images_prepare_and_none_is_skipped_by_name_or_inline_flag(self):
        from rfqparse.ingest import ingest_eml
        seen = 0
        for eml in sorted(self.EMAILS.glob("*.eml")):
            for a in ingest_eml(eml).attachments:
                if a.content_type.startswith("image/"):
                    seen += 1
                    r = I.prepare_image(a.data)
                    self.assertTrue(r.ok, f"{eml.stem}/{a.filename}: {r.reason}")
                    self.assertLessEqual(max(r.width, r.height), config.IMAGE_MAX_EDGE)
        self.assertEqual(seen, 5)


if __name__ == "__main__":
    unittest.main()
