"""Deployment constants. Kept out of the code that uses them."""

# Mail from exactly this domain is one of our own reps (README: "World facts").
RIDGELINE_DOMAIN = "ridgeline-supply.example"

# PDF rendering (rfqparse/render/pdf.py)
PDF_MAX_PAGES = 15        # pages read per PDF; more than this returns the first pages, flagged truncated
PDF_SCAN_MIN_CHARS = 25   # a page with fewer non-space text characters than this is treated as scanned
PDF_RASTER_DPI = 150      # resolution of the PNG made for pages that need vision
