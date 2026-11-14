# Edge cases

## General properties

Rules that do not depend on dev strings. Use these to write the prompt and the checks.

1. Classify by what the current message does and by the direction of the business transaction. Do not classify from the subject alone and do not classify from the sender domain alone.
2. Count items only from the operative content of the current message: the current body, its attachments, pasted images, and forwarded customer content when a Ridgeline rep forwards a customer email for action.
3. Do not count items in quoted earlier history unless the current message expressly adopts selected items by reference (dev evidence: E020).
4. Do not count headings, notes, totals, signatures, logos, prices, or superseded document versions as items.
5. Every description must be self contained. Carry in the subject line context, headings, blanket notes, column headers, sheet names, section headings, and earlier defining lines that apply to that item. Do not carry context across a section boundary.
6. Quantity and unit stay as the customer stated them, subject to the allowed unit list. A vague amount gives quantity null. A size, schedule, length, rating, part number, or price is never a quantity.
7. Source is body or the exact attachment or image filename.
8. needs_review is only for rfq, rfq_revision, and purchase_order. Use it when the pointed attachment is missing or cannot be opened, or when the item is identified only by something outside the email. Still return whatever lines can be read.
9. A revision outputs the complete order as now intended, and the earlier email keeps its own original lines. A duplicate gets the same classification as the original, duplicate_of set to the original, and zero line items.
10. An attachment or image that opens but cannot be read (blurry photo, illegible scan) goes to needs_review (rfq, rfq_revision, purchase_order only), with whatever lines could be read still returned.
11. A revision whose parent email is not in the inbox gets revises null.
12. When the model has doubt, it says why in a structured reason, and code turns that into needs_review. If the doubt is request vs non-request, classify as the request and flag it, since needs_review only exists for rfq, rfq_revision and purchase_order. Still return whatever lines were readable.

## Per-email notes (dev evidence only)

These describe the 24 dev emails. They are evidence for the rules above, not rules to encode.

**E001:** Baseline body RFQ with messy quantity placement. Quantities appear before items, in parentheses, and at the end after x. Pipe joints are counted with unit EA because joints are not in the allowed unit list, while the galvanized pipe line is FT.

**E002:** The defining context lives in the subject line: all items are 316 stainless threaded fittings. Lines are relative, including same size and a couple caps. Carry 316 and threaded into every description. A couple is vague, so that line has quantity null and unit null.

**E003:** Section context splits three ways. Pipe lines take SCH 40, A53B, ERW, and threaded and coupled from the pipe note, and must not take malleable or 150#. Fitting lines take malleable iron, black, and 150#, and must not take the pipe specs. Valve lines take only the valve words, bronze and 125#, and no heading context.

**E004:** Revision of E003. Output the complete order as now intended: the 3 inch pipe quantity changes from 10 to 14, the unions are removed, and a new ball valve line is added. The result still has 8 items. Set revises to E003. E003 itself stays unchanged. The revision descriptions still need the E003 heading and note context.

**E005:** Excel edge case. Use only the visible Rev B sheet in Tank14_Piping_RevB.xlsx. The hidden Rev A sheet is superseded and counts for nothing. The blanket note gives butt weld, SCH 40, and A234 WPB to the fittings, but the 6 x 4 reducer expressly says SCH 80 and must not carry SCH 40. The TOTAL QTY row is a sum, not an item.

**E006:** Merge two sources. The PDF form header gives A105, 150#, and raised face to all 42 flange rows. The email body adds 2 spiral wound gasket lines that are not in the PDF, and those body lines must not carry A105. Total is 44 items, not 42.

**E007:** Handwritten photo list in IMG_4172.jpeg. Vision has to read handwriting and ditto marks. The tee line repeats the 2 inch galvanized context, and the last nipple line repeats the galvanized nipple context. Source is the JPEG for all 7 items.

**E008:** Resend of E006. The later email is the duplicate. Classification stays rfq, duplicate_of is E006, and line items are empty. Do not count the 44 items twice.

**E009:** Scanned PDF with no text layer. Text extraction returns nothing, so the pages must go through vision. Expand the abbreviations in descriptions: WN, SO, and BL rows are flanges, the spiral wound rows are gaskets, and the B7 rows are studs. All 32 items come from Scan_RFQ_SC-4418.pdf.

**E010:** Two images with opposite roles. image001.png is only the Delta Mechanical logo and holds no items. image002.png is a spreadsheet screenshot and holds all 6 items. Spring nuts use unit BX. The all thread rod line is 12 EA; the 6 ft in that line is a length spec, not the quantity unit.

**E011:** Direction edge case. Sabine Valve is a supplier quoting Ridgeline, so classification is vendor_quote and line items are empty, even though the current message contains a priced item table. The quoted Ridgeline request below also counts for nothing.

**E012:** Status inquiry. The current message only asks for an update on an earlier Ridgeline quote. The item list visible below is the quoted original RFQ and counts for nothing. Classification is status_inquiry with zero items.

**E013:** Auto reply edge case. This is a delivery failure notice, classification auto_reply, zero items. The attached original message is an outbound Ridgeline email asking a supplier for a quote. Never recurse into it as if it were a customer request, or the parser invents a phantom RFQ.

**E014:** Other. A mass sale flyer from a vendor warehouse. The priced flange lines are the seller advertising its own stock, not anyone asking Ridgeline for anything. Zero items. This differs from E011, where a supplier answers Ridgeline directly and the class is vendor_quote.

**E015:** Purchase order in a PDF while the body has no items. Extract all 6 lines from PO_TPS-88213.pdf. The pipe line is 210 FT, not EA. Unit prices and extended prices are not quantities. The body mention of an earlier quote does not make this an RFQ.

**E016:** Forwarded customer RFQ. Marcus Doyle is a Ridgeline rep, but he is only forwarding Priya Raman at Hollis Mechanical. Classify by her request, rfq, and read his note as well. The CSV puts size in its own column, so every description must combine the size with the item words. The job number and ship location are context, not items.

**E017:** Missing content edge case. The body says the Job 2291 BOM is attached, but the only image is the Cobalt Ridge logo. Classification is rfq, line items are empty, needs_review is true, and the review reason is that the promised BOM is not present, only a logo. Do not invent items from the job name.

**E018:** Body RFQ where the size sits at the end of most lines and must be joined into each description. The PTFE thread tape line has no piping size and must not be given one. Its unit is RL, and the quantity is 12.

**E019:** Word document with two context zones. The pipe rows take the document pipe context: carbon steel, pipe, SCH 80, A106B, seamless. The fitting rows take socket weld, 3000#, and A105, and must not take SCH 80 or A106. Apply context by section, never globally.

**E020:** Purchase order by reference. Janelle says go ahead with items 1, 2, and 4 from the quoted Ridgeline quote, and holds 3, 5, and 6. Classification is purchase_order with exactly those 3 items. Do not extract all 6 quoted lines, and do not extract the held items. This is the exception where quoted content counts, because the current message adopts selected lines by number.

**E021:** Largest Excel BOM, 50 items in SR-4-117_Unit4_BOM.xlsx. Item numbers restart in the fittings section, so never deduplicate by item number alone. Pipe rows take pipe, SCH 80, and seamless context. Fitting rows take socket weld and 3000# context and must not take SCH 80. End types such as PE, TOE, and BE belong in the pipe descriptions.

**E022:** Locked PDF edge case. The RFQ attachment AldenMarine_RFQ_AM-26-0412.pdf is protected by a password, and the password is not in this inbox. Classification is rfq, line items are empty, needs_review is true, and the review reason is that the attachment cannot be opened. Do not crash and do not invent items.

**E023:** Item identified by a photo that is inside the email. The body asks for 3 of the valve in IMG_4417.jpg. Read the nameplate and the valve in the image: model HV-800-FGS, a 2 inch 800 class A105 gate valve with 13Cr trim and NPT ends. One line item, quantity 3 EA, source IMG_4417.jpg, needs_review false, because the identifying evidence is in the email itself.

**E024:** Terse table with global context. The table rows give only the item type and size, while the email text gives the shared specs: 316, threaded, and 3000#. Every one of the 6 descriptions must carry those shared specs.
