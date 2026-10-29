"""P2.06: reciprocal rank fusion, the vector text form, and the real embedding model."""

import os

import pytest

from scc_core.pipeline import embeddings


def test_fusion_rewards_passages_both_rankings_like():
    # Passage 3 is second by keywords and first by meaning; it beats 1 (first by keywords, last by meaning).
    assert embeddings.fuse([1, 3, 2], [3, 2, 1], weight=0.5, k=60)[0] == 3


def test_fusion_weight_decides_who_wins_a_disagreement():
    keyword, vector = [1, 2], [2, 1]
    assert embeddings.fuse(keyword, vector, weight=1.0, k=60) == [1, 2]
    assert embeddings.fuse(keyword, vector, weight=0.0, k=60) == [2, 1]
    assert embeddings.fuse(keyword, vector, weight=0.7, k=60) == [1, 2]


def test_fusion_ties_go_to_the_earlier_passage():
    assert embeddings.fuse([5, 4], [4, 5], weight=0.5, k=60) == [4, 5]


def test_fusion_rejects_a_weight_outside_0_to_1():
    with pytest.raises(ValueError):
        embeddings.fuse([1], [1], weight=1.5, k=60)


def test_vector_literal_and_text_hash_are_stable():
    assert embeddings.literal([0.1, -0.25]) == "[0.100000,-0.250000]"
    assert embeddings.text_hash("é") == embeddings.text_hash("é")
    assert len(embeddings.text_hash("x")) == 64


def _model_or_skip():
    if not (embeddings.model_dir() / "model_optimized.onnx").exists():
        if os.environ.get("CI"):
            pytest.fail(
                "CI must fetch the embedding model (tools/fetch_embedding_model.py)"
            )
        pytest.skip("no embedding model: run tools/fetch_embedding_model.py")
    return embeddings.load("BAAI/bge-small-en-v1.5")


def test_real_model_ranks_a_paraphrase_above_an_unrelated_passage():
    model = _model_or_skip()
    query = model.query("fish oil lowers triglycerides")
    near, far = model.passages(
        [
            "Omega-3 supplementation reduced serum triacylglycerol concentrations by 15% versus placebo.",
            "Participants were recruited from two outpatient clinics between 2015 and 2017.",
        ]
    )

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        return dot / (sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)

    assert len(query) == 384
    assert cosine(query, near) > cosine(query, far) + 0.1
