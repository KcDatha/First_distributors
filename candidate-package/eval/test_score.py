#!/usr/bin/env python3
"""Mutation tests for score.py: start from a perfect output, break one thing, assert the right
number moves (and the others don't).

    python eval/test_score.py -v
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score  # noqa: E402

KEY_PATH = Path(__file__).resolve().parent.parent / "data" / "dev" / "answer_key.json"
KEY = score.load(KEY_PATH)


def perfect_output(key: dict) -> dict:
    """What a perfect parser would emit; description = raw + first alternative of each must_mention."""
    out = {}
    for i, e in key.items():
        lines = [{
            "raw": l["raw"],
            "description": l["raw"] + " " + " ".join(p.split("|")[0] for p in l["must_mention"]),
            "qty": l["qty"], "uom": l["uom"], "source": l["source"],
        } for l in e["line_items"]]
        out[i] = {"classification": e["classification"], "revises": e["revises"],
                  "duplicate_of": e["duplicate_of"], "needs_review": e["needs_review"],
                  "review_reason": "x" if e["needs_review"] else None, "line_items": lines}
    return out


def first_line_idx(key: dict, pred) -> tuple[str, int]:
    for i in sorted(key):
        for n, l in enumerate(key[i]["line_items"]):
            if pred(l):
                return i, n
    raise AssertionError("no matching key line")


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.out = perfect_output(KEY)
        self.base = score.compute(KEY, self.out)

    def run_m(self, out=None, key=None):
        return score.compute(key or KEY, out or self.out)

    def test_baseline_is_perfect(self):
        b = self.base
        self.assertEqual((b["silent"], b["halluc"], b["lost"], b["phantom"], b["cls_wrong"], b["missed"]),
                         ([], [], [], [], [], []))
        for f in ("qty", "uom", "src", "ctx"):
            self.assertEqual(b[f], [210, 210], f)

    def test_drop_one_line_lowers_recall(self):
        self.out["E001"]["line_items"].pop(0)
        m = self.run_m()
        self.assertEqual(m["n_paired"], self.base["n_paired"] - 1)
        self.assertEqual(len(m["missed"]), 1)
        self.assertEqual(m["halluc"], [])            # precision untouched
        self.assertEqual(m["n_our"], m["n_paired"])

    def test_junk_line_raises_hallucinated(self):
        self.out["E001"]["line_items"].append(
            {"raw": "zzz qqq junk", "description": "junk", "qty": 1, "uom": "EA", "source": "body"})
        m = self.run_m()
        self.assertEqual(m["halluc"], [("E001", "zzz qqq junk")])
        self.assertEqual(m["n_paired"], 210)         # recall untouched

    def test_qty_beyond_tolerance_lowers_qty_only(self):
        i, n = first_line_idx(KEY, lambda l: l["qty"] is not None)
        self.out[i]["line_items"][n]["qty"] += 1     # tol is 0 in the dev key
        m = self.run_m()
        self.assertEqual(m["qty"], [209, 210])
        self.assertEqual((m["uom"], m["src"], m["ctx"]), ([210, 210],) * 3)

    def test_qty_tolerance_is_relative(self):
        key = copy.deepcopy(KEY)
        i, n = first_line_idx(key, lambda l: l["qty"] and l["qty"] >= 10)
        key[i]["line_items"][n]["qty_tol"] = 0.1
        q = key[i]["line_items"][n]["qty"]
        self.out[i]["line_items"][n]["qty"] = q * 1.05
        self.assertEqual(self.run_m(key=key)["qty"], [210, 210])
        self.out[i]["line_items"][n]["qty"] = q * 1.20
        self.assertEqual(self.run_m(key=key)["qty"], [209, 210])

    def test_null_qty_unit_and_unscored(self):
        key = copy.deepcopy(KEY)
        i, n = first_line_idx(key, lambda l: l["qty"] is not None)
        key[i]["line_items"][n]["qty_tol"] = None    # unscored
        self.out[i]["line_items"][n]["qty"] = 999999
        self.assertEqual(self.run_m(key=key)["qty"], [209, 209])

    def test_uom_null_counts_as_ea(self):
        i, n = first_line_idx(KEY, lambda l: l["uom"] == "EA")
        self.out[i]["line_items"][n]["uom"] = None
        self.assertEqual(self.run_m()["uom"], [210, 210])
        self.out[i]["line_items"][n]["uom"] = "FT"
        self.assertEqual(self.run_m()["uom"], [209, 210])

    def test_wrong_source_lowers_source(self):
        self.out["E001"]["line_items"][0]["source"] = "other.pdf"
        self.assertEqual(self.run_m()["src"], [209, 210])

    def test_remove_must_mention_lowers_context(self):
        i, n = first_line_idx(KEY, lambda l: len(l["must_mention"]) >= 2)
        mm = KEY[i]["line_items"][n]["must_mention"]
        kept = [p.split("|")[0] for p in mm[:-1]]
        self.out[i]["line_items"][n]["description"] = "item " + " ".join(kept)  # drops the last phrase
        self.assertFalse(score.phrase_in(mm[-1], score.norm_tokens(" ".join(kept))))  # test setup sanity
        m = self.run_m()
        self.assertEqual(m["ctx"], [209, 210])
        self.assertEqual((m["qty"], m["uom"], m["src"]), ([210, 210],) * 3)

    def test_add_must_not_mention_lowers_context(self):
        i, n = first_line_idx(KEY, lambda l: l["must_not_mention"])
        bad = KEY[i]["line_items"][n]["must_not_mention"][0]
        self.out[i]["line_items"][n]["description"] += " " + bad
        self.assertEqual(self.run_m()["ctx"], [209, 210])

    def test_request_classified_other_raises_lost(self):
        self.out["E001"]["classification"] = "other"
        m = self.run_m()
        self.assertEqual(m["lost"], ["E001"])
        self.assertEqual(m["cls_wrong"], [("E001", "rfq", "other")])
        self.assertEqual(m["phantom"], [])

    def test_non_request_classified_rfq_raises_phantom(self):
        self.out["E011"]["classification"] = "rfq"
        m = self.run_m()
        self.assertEqual(m["phantom"], ["E011"])
        self.assertEqual(m["lost"], [])

    def test_clear_lines_and_review_raises_silent_loss(self):
        self.out["E001"]["line_items"] = []
        self.out["E001"]["needs_review"] = False
        self.assertEqual(self.run_m()["silent"], ["E001"])
        # a review flag instead of lines is NOT a silent loss
        self.out["E001"]["needs_review"] = True
        self.assertEqual(self.run_m()["silent"], [])
        # needs_review key email with no lines: dropping the flag is a silent loss
        self.out["E022"]["needs_review"] = False
        self.assertEqual(self.run_m()["silent"], ["E022"])

    def test_missing_email_is_silent_loss(self):
        del self.out["E001"]
        m = self.run_m()
        self.assertEqual(m["missing"], ["E001"])
        self.assertIn("E001", m["silent"])


class ContextMatchingTests(unittest.TestCase):
    def ctx(self, mention, desc, must_not=()):
        return score.context_ok({"must_mention": list(mention), "must_not_mention": list(must_not)},
                                {"description": desc})

    def test_1500_is_not_150(self):
        """Swap 150# for 1500# in a real line's description: context must fail."""
        i, n = first_line_idx(KEY, lambda l: any(p.startswith("150#") for p in l["must_mention"]))
        line = KEY[i]["line_items"][n]
        good = perfect_output(KEY)
        good_desc = good[i]["line_items"][n]["description"]
        self.assertTrue(score.context_ok(line, {"description": good_desc}))
        bad_desc = good_desc.replace("150#", "1500#")
        self.assertNotEqual(bad_desc, good_desc)
        bad = copy.deepcopy(good)
        bad[i]["line_items"][n]["description"] = bad_desc
        self.assertEqual(score.compute(KEY, bad)["ctx"], [209, 210])

    def test_150_spellings_equal(self):
        for d in ("150#", "150 lb", "150lbs", "Class 150", "class150 RF"):
            self.assertTrue(self.ctx(["150#"], f"flange {d} A105"), d)

    def test_1500_does_not_satisfy_150_and_not_must_not(self):
        self.assertFalse(self.ctx(["150#"], "flange 1500# A105"))
        self.assertTrue(self.ctx([], "flange 1500# A105", must_not=["150#"]))   # must_not not triggered
        self.assertFalse(self.ctx([], "flange 150# A105", must_not=["150#"]))   # but really 150 triggers it

    def test_sch_40_vs_400(self):
        self.assertFalse(self.ctx(["sch 40"], "pipe SCH 400"))
        self.assertTrue(self.ctx([], "pipe SCH 400", must_not=["sch 40"]))
        for d in ("SCH 40", "Sch40", "schedule 40", "Schedule 40"):
            self.assertTrue(self.ctx(["sch 40|schedule 40"], f"pipe {d}"), d)

    def test_red_mi_cs_whole_words_only(self):
        self.assertFalse(self.ctx(["red"], "shredded pipe"))
        self.assertFalse(self.ctx(["mi"], "admission tee"))
        self.assertFalse(self.ctx(["cs"], "mocs fitting"))
        self.assertTrue(self.ctx(["red"], "RED 6 x 4"))
        self.assertTrue(self.ctx(["reducer"], "RED 6 x 4"))
        self.assertTrue(self.ctx(["malleable iron"], "MI tee"))
        self.assertTrue(self.ctx(["carbon steel"], "CS pipe"))

    def test_other_equivalences_and_alternatives(self):
        self.assertTrue(self.ctx(["bw"], "butt weld elbow"))
        self.assertTrue(self.ctx(["threaded"], "NPT coupling"))
        self.assertTrue(self.ctx(["seamless"], "SMLS pipe"))
        self.assertTrue(self.ctx(["galvanized"], "galv nipple"))
        self.assertTrue(self.ctx(["t&c|threaded and coupled"], "pipe T&C"))
        self.assertFalse(self.ctx(["a53b"], "pipe A53"))


if __name__ == "__main__":
    unittest.main()
