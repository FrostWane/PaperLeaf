from paperleaf_api.rag.rrf import RankedHit, reciprocal_rank_fusion


def test_rrf_rewards_hits_from_multiple_channels() -> None:
    vector = [RankedHit("a", 0.99), RankedHit("b", 0.8), RankedHit("c", 0.7)]
    keyword = [RankedHit("b", 9.0), RankedHit("d", 8.0), RankedHit("a", 7.0)]

    fused = reciprocal_rank_fusion([vector, keyword], rank_constant=60, limit=4)

    assert [hit.id for hit in fused[:2]] == ["b", "a"]
    assert fused[0].score > fused[2].score


def test_rrf_ignores_duplicates_inside_one_channel() -> None:
    ranking = [RankedHit("a", 2), RankedHit("a", 1), RankedHit("b", 0)]
    fused = reciprocal_rank_fusion([ranking], rank_constant=10, limit=2)
    assert [hit.id for hit in fused] == ["a", "b"]
