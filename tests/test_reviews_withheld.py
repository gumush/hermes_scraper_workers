"""
"Rating without a review count" is a distinct page state and the tests pin
down where its edges are — measured on captured DOM: twenty captures of two
places showed a score with no count, zero review cards and no Reviews tab,
while the same places from a residential address carried "4.6(16)".
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workers.engine.scraper import GoogleReviewsScraper as S  # noqa: E402

withheld = S._reviews_withheld


@pytest.mark.parametrize("text", ["4.6", "4,4", "3.9 ", "5.0\nBar"])
def test_score_without_a_count_is_withheld(text):
    assert withheld(text) is True


@pytest.mark.parametrize("text", [
    "4.6(16)", "4.6 (16)", "4,6(1.234)", "3.9\n(2 456)", "5.0(1)",
])
def test_score_with_a_count_is_not_withheld(text):
    """The ordinary rendering: score and how many. Nothing is being withheld."""
    assert withheld(text) is False


@pytest.mark.parametrize("text", ["", "   ", "\n", "Bar", "Coffee shop"])
def test_no_score_at_all_is_not_withheld(text):
    """
    A business with no reviews has no score either — that is the review-less
    case, which must stay a success rather than becoming a failure.
    """
    assert withheld(text) is False
