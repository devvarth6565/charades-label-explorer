import shutil
import tempfile
import unittest
from pathlib import Path

from explorer.charades import load_dataset
from explorer.index import build_index, mark_videos
from explorer.search import Query, QueryError, Store

FIXTURES = Path(__file__).parent / "fixtures" / "charades_mini"
ALL_IDS = ["AAA01", "AAA02", "AAA03", "AAA04", "AAA05", "BBB01", "BBB02"]


def make_store(tmp: Path, use_fts: bool = True) -> Store:
    db = tmp / ("fts.db" if use_fts else "plain.db")
    build_index(load_dataset(FIXTURES), db, use_fts=use_fts)
    mark_videos(db, ["AAA01"])
    return Store(db)


def params(**kwargs):
    return {k: (v if isinstance(v, list) else [str(v)]) for k, v in kwargs.items()}


class SearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.store = make_store(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def ids(self, **kwargs):
        result = self.store.search(Query.from_params(params(**kwargs)))
        return [clip["id"] for clip in result["items"]]

    def test_no_filters_returns_everything_by_id(self):
        self.assertTrue(self.store.fts)
        self.assertEqual(self.ids(), ALL_IDS)

    def test_text_search_is_stemmed(self):
        self.assertEqual(self.ids(q="cooks"), ["AAA01"])

    def test_half_typed_word_is_expanded(self):
        # "cooki" is not a prefix of the stemmed token "cook"; vocab expansion fixes that
        self.assertEqual(self.ids(q="cooki"), ["AAA01"])
        # a finished word (trailing space) is not treated as a prefix
        self.assertEqual(self.ids(q="coo "), [])

    def test_phrase_and_multiword(self):
        self.assertEqual(self.ids(q='"cooking at the stove"'), ["AAA01"])
        # phrases are stemmed too: matches the label "Drinking from a cup"
        self.assertEqual(self.ids(q='"drinks from a cup"'), ["AAA01", "BBB01", "AAA04"])
        self.assertEqual(self.ids(q="door chair"), ["AAA02"])

    def test_search_by_clip_id_and_label(self):
        self.assertEqual(self.ids(q="bbb01"), ["BBB01"])
        self.assertIn("AAA04", self.ids(q="watching"))

    def test_hostile_query_syntax_does_not_crash(self):
        for text in ['NEAR( "x', "AND OR NOT", "*", '"', "c001:", "'; DROP TABLE clip; --"]:
            self.store.search(Query.from_params(params(q=text)))

    def test_snippet_marks_the_match(self):
        result = self.store.search(Query.from_params(params(q="stove")))
        self.assertIn("\x02stove\x03", result["items"][0]["snippet"])

    def test_scene_is_or(self):
        self.assertEqual(self.ids(scene=["Kitchen", "Garage"]), ["AAA01", "AAA04", "BBB01", "BBB02"])

    def test_actions_are_and(self):
        self.assertEqual(self.ids(action="c003"), ["AAA01", "AAA04", "BBB01"])
        self.assertEqual(self.ids(action=["c003", "c004"]), ["AAA01"])
        self.assertEqual(self.ids(action="c003,c004"), ["AAA01"])

    def test_verb_and_object(self):
        self.assertEqual(self.ids(verb="drink"), ["AAA01", "AAA04", "BBB01"])
        self.assertEqual(self.ids(object="door"), ["AAA02", "AAA05", "BBB01"])

    def test_simple_filters(self):
        self.assertEqual(self.ids(split="test"), ["BBB01", "BBB02"])
        self.assertEqual(self.ids(verified="no"), ["AAA03"])
        self.assertEqual(self.ids(min_length=10, max_length=18), ["AAA01", "AAA03", "AAA04"])
        self.assertEqual(self.ids(min_quality=6), ["AAA01", "AAA05", "BBB01"])
        self.assertEqual(self.ids(has_video=1), ["AAA01"])

    def test_issue_filters(self):
        self.assertEqual(self.ids(issue="any"), ["AAA03", "AAA04", "AAA05"])
        self.assertEqual(self.ids(issue="none"), ["AAA01", "AAA02", "BBB01", "BBB02"])
        self.assertEqual(self.ids(issue="inverted_segment"), ["AAA04"])
        self.assertEqual(self.ids(issue=["no_actions", "segment_overrun"]), ["AAA03", "AAA05"])

    def test_sorting(self):
        self.assertEqual(self.ids(sort="longest")[0], "AAA05")
        self.assertEqual(self.ids(sort="fewest_actions")[0], "AAA03")
        self.assertEqual(self.ids(sort="most_issues")[0], "AAA04")

    def test_pagination_clamps_to_last_page(self):
        result = self.store.search(Query.from_params(params(page_size=3, page=99)))
        self.assertEqual((result["page"], result["pages"], result["total"]), (3, 3, 7))
        self.assertEqual([c["id"] for c in result["items"]], ["BBB02"])

    def test_totals(self):
        result = self.store.search(Query.from_params(params(split="test")))
        self.assertEqual(result["segments"], 3)
        self.assertAlmostEqual(result["hours"], round((9.5 + 7.25) / 3600, 2))

    def test_facets_are_disjunctive_for_scene(self):
        facets = self.store.search(Query.from_params(params(scene="Kitchen")))["facets"]
        scenes = {f["value"]: f["count"] for f in facets["scene"]}
        self.assertEqual(scenes["Garage"], 1)  # still visible while Kitchen is selected
        verbs = {f["value"]: f["count"] for f in facets["verb"]}
        self.assertEqual(verbs["drink"], 3)  # counted within the Kitchen results only
        self.assertNotIn("sit", verbs)

    def test_invalid_params_raise_query_error(self):
        for bad in (dict(split="val"), dict(sort="random"), dict(min_quality=9),
                    dict(page=0), dict(min_length="abc"), dict(issue="bogus"),
                    dict(verified="maybe"), dict(page_size=1000)):
            with self.assertRaises(QueryError, msg=str(bad)):
                Query.from_params(params(**bad))

    def test_get_clip_detail(self):
        clip = self.store.get_clip("AAA01")
        self.assertEqual(len(clip["descriptions"]), 2)
        self.assertEqual(clip["raw_actions"], "c004 0.00 12.50;c003 11.00 15.20")
        self.assertTrue(clip["has_video"])
        self.assertIsNone(self.store.get_clip("NOPE1"))

    def test_iter_matches_returns_all_rows(self):
        rows = list(self.store.iter_matches(Query.from_params(params(object="door", page_size=1))))
        self.assertEqual([r["id"] for r in rows], ["AAA02", "AAA05", "BBB01"])

    def test_meta(self):
        meta = self.store.meta()
        self.assertEqual(meta["stats"]["clips"], 7)
        self.assertEqual(meta["stats"]["with_video"], 1)
        self.assertEqual(meta["stats"]["with_issues"], 3)
        issues = {i["value"]: i["count"] for i in meta["issues"]}
        self.assertEqual(issues["no_actions"], 1)
        door = next(c for c in meta["classes"] if c["id"] == "c001")
        self.assertEqual(door["count"], 1)


class FallbackSearchTest(unittest.TestCase):
    """Same queries against an index built without FTS5."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.store = make_store(cls.tmp, use_fts=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def ids(self, **kwargs):
        return [c["id"] for c in self.store.search(Query.from_params(params(**kwargs)))["items"]]

    def test_substring_search(self):
        self.assertFalse(self.store.fts)
        self.assertEqual(self.ids(q="cook"), ["AAA01"])
        self.assertEqual(self.ids(q="door chair"), ["AAA02"])

    def test_like_wildcards_are_escaped(self):
        self.assertEqual(self.ids(q="100%"), [])
        self.assertEqual(self.ids(q="_"), ALL_IDS)  # no word characters -> no filter

    def test_filters_still_work(self):
        self.assertEqual(self.ids(q="cook", scene="Garage"), [])
        self.assertEqual(self.ids(verb="drink", split="test"), ["BBB01"])


if __name__ == "__main__":
    unittest.main()
