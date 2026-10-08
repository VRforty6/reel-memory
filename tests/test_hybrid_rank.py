"""Unit tests: RRF fusion ranking (PRD §26). Pure logic — no DB."""

from app.search.hybrid import rrf_merge


def test_item_in_two_lists_outranks_single_list():
    merged = dict(rrf_merge([["a", "b"], ["b", "c"]]))
    assert merged["b"] > merged["a"]
    assert merged["b"] > merged["c"]


def test_rank_order_within_list_matters():
    merged = rrf_merge([["a", "b", "c"]])
    ids = [doc_id for doc_id, _ in merged]
    assert ids == ["a", "b", "c"]


def test_smaller_k_weights_top_ranks_more_steeply():
    gentle = dict(rrf_merge([["x"], ["y", "x"]], k=60))
    steep = dict(rrf_merge([["x"], ["y", "x"]], k=1))
    # with k=1, x's rank-1 hit dominates y's rank-1 hit much more strongly
    assert (steep["x"] - steep["y"]) > (gentle["x"] - gentle["y"])


def test_empty_inputs():
    assert rrf_merge([]) == []
    assert rrf_merge([[], []]) == []


def test_duplicate_ids_inside_one_list_count_once_per_position():
    merged = dict(rrf_merge([["a", "a"]]))
    # rank1 + rank2 contributions, still a single entry
    assert len(merged) == 1
    assert merged["a"] == 1 / 61 + 1 / 62
