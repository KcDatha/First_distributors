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

# USD per 1,000,000 tokens, keyed by model name. A model missing here gets usd = null in the ledger and
# the client refuses to call it (the spend guard could not count it).
PRICES_ARE_PLACEHOLDER = False
MODEL_PRICES = {
    "gpt-4.1-mini": {"input_per_1m": 0.40, "output_per_1m": 1.60},   # text only
    "gpt-4.1":      {"input_per_1m": 2.00, "output_per_1m": 8.00},   # anything with an image in the payload
}

# Which model handles which email (rfqparse/llm/routing.py). Routed by what is actually in the payload.
ROUTING = {
    "text_model": "gpt-4.1-mini",     # no image part in the payload
    "image_model": "gpt-4.1",         # at least one image part in the payload
    "escalate_text_model_to": "gpt-4.1",   # one retry here when the text model's answer fails validation
}

# OpenAI client (rfqparse/llm/client.py)
LLM_TIMEOUT_S = 60.0              # read/total timeout per request
LLM_CONNECT_TIMEOUT_S = 10.0
LLM_MAX_ATTEMPTS = 3              # our own retries (the SDK's are off); counts the first try
LLM_BACKOFF_BASE_S = 1.0          # delay before retry n is about base * 2**(n-1), capped, with jitter
LLM_BACKOFF_MAX_S = 20.0
LLM_IMAGE_DETAIL = "high"         # detail setting for images sent to the model (it is part of the cache key)
MAX_USD_PER_RUN = 2.00            # real calls stop (BudgetExceeded) once THIS RUN's spend reaches it (no cap across runs)

# Per-model request settings. temperature None = do not send one (models that reject it).
# Both models take temperature 0; the output limit is generous because one reply can list 40+ items.
DEFAULT_MODEL_SETTINGS = {"temperature": 0, "max_completion_tokens": 8192}
MODEL_SETTINGS = {
    "gpt-4.1-mini": {"temperature": 0, "max_completion_tokens": 8192},
    "gpt-4.1":      {"temperature": 0, "max_completion_tokens": 8192},
}
