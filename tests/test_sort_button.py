"""
Maps writes the place's own name into the labels of its controls, so a word
list checked as a substring reads the name as the control.

Found the hard way: a venue inside a hotel offered "Located in: Santa Marina,
a Luxury Collection Resort" as its sort button, because "sort" hides inside
"Resort". Clicking it navigated to the hotel, and the review stage then read
the hotel's page and collected nothing for the place it was asked about. On
the captured DOM the old rule produced five candidates, every one of them the
hotel's own controls.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workers.engine.scraper import (  # noqa: E402
    NON_SORT_WORDS, _has_any_word, _has_sort_word)


@pytest.mark.parametrize("label", [
    "Sort", "Sort reviews", "sort", "Sırala", "Yorumları sırala",
    "Sortieren", "Trier", "Ordenar", "排序",
    "Sort reviews for Backyard by Olde",
])
def test_real_sort_labels_match(label):
    assert _has_sort_word(label) is True


@pytest.mark.parametrize("label", [
    # Every one of these was offered as the sort control on a captured page.
    "Located in: Santa Marina, a Luxury Collection Resort, Mykonos",
    "Overview of Santa Marina, a Luxury Collection Resort, Mykonos",
    "Photos of Santa Marina, a Luxury Collection Resort, Mykonos",
    "Prices for Santa Marina, a Luxury Collection Resort, Mykonos",
    "Reviews for Santa Marina, a Luxury Collection Resort, Mykonos",
    # and the same trap in other shapes
    "Resort", "resorts nearby", "Assorted plates", "Consort Hotel",
])
def test_names_containing_the_word_do_not_match(label):
    assert _has_sort_word(label) is False


@pytest.mark.parametrize("label", ["Back", "Close", "Cancel", "Next", "Previous"])
def test_negative_words_still_reject_their_own_controls(label):
    assert _has_any_word(label, NON_SORT_WORDS) is True


@pytest.mark.parametrize("label", [
    # A real place: rejecting this label threw away a working sort button.
    "Backyard by Olde",
    "Sort reviews for Backyard by Olde",
    "Closerie Bistro", "Nextdoor Coffee", "Cancelleria",
])
def test_names_containing_a_negative_word_are_not_rejected(label):
    assert _has_any_word(label, NON_SORT_WORDS) is False


def test_empty_text_matches_nothing():
    assert _has_sort_word("") is False
    assert _has_sort_word(None) is False
    assert _has_any_word("", NON_SORT_WORDS) is False
