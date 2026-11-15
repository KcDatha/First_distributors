"""Deployment constants. Kept out of the code that uses them."""

# Mail from exactly this domain is one of our own reps (README: "World facts").
RIDGELINE_DOMAIN = "ridgeline-supply.example"

# PDF rendering (rfqparse/render/pdf.py)
PDF_MAX_PAGES = 15        # pages read per PDF; more than this returns the first pages, flagged truncated
PDF_SCAN_MIN_CHARS = 25   # a page with fewer non-space text characters than this is treated as scanned
PDF_RASTER_DPI = 150      # resolution of the PNG made for pages that need vision
PDF_MIXED_IMAGE_RATIO = 0.5  # a page whose images cover at least this share of its area is rasterized too ("mixed")

# Image preparation (rfqparse/render/image.py)
IMAGE_MAX_EDGE = 2000     # longest side after downscaling, in pixels
IMAGE_MIN_EDGE = 16       # an image with a side shorter than this is skipped (tracking pixel, spacer)
IMAGE_JPEG_QUALITY = 90
IMAGE_MAX_PIXELS = 100_000_000  # refuse to decode anything with more pixels than this

# LLM cache and cost (rfqparse/llm/)
CACHE_DIR = None                  # None = cache/ at the repo root (found relative to the package, never the cwd)
LEDGER_PATH = None                # None = cost_ledger.jsonl at the repo root

# USD per 1,000,000 tokens, keyed by model name. PLACEHOLDER NUMBERS, NOT REAL PRICES: fill this in
# when the model is chosen. A model that is missing here gets usd = null and the cost report says so.
PRICES_ARE_PLACEHOLDER = True
MODEL_PRICES = {
    "placeholder-model": {"input_per_1m": 1.00, "output_per_1m": 2.00},
}
