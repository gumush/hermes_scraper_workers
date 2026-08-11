"""
Hotel place pages render review cards differently, and the differences were
found by reading a live page (Odile Konak, ChIJ-3SMeg-QwxQR4VWYNSsgqhg) rather
than guessed:

  * the score is plain text, "5/5", in a span with no aria-label and no
    role="img" — both RATING_SELECTORS returned nothing on all 20 cards, so
    every hotel review was stored with rating 0.0
  * the date line names the source, "3 years ago on Google" /
    "7 years ago on Tripadvisor" — that page's visible cards were 7
    Tripadvisor to 3 Google, so a hotel's review list is not purely Google's

These tests pin both down against fakes shaped like the real card.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workers.engine.models import RawReview  # noqa: E402


class FakeEl:
    """Minimal stand-in for a WebElement: text plus children."""

    def __init__(self, text="", children=None):
        self.text = text
        self._children = children or []

    def find_elements(self, _by, css):
        if css == "*":
            return list(self._children)
        if css == "span":
            return [c for c in self._children if isinstance(c, FakeEl)]
        return []

    def find_element(self, _by, css):
        found = self.find_elements(_by, css)
        if not found:
            raise LookupError(css)
        return found[0]


def card_with(*spans):
    return FakeEl(children=[FakeEl(s) for s in spans])


def test_reads_the_score_written_as_text():
    assert RawReview._rating_from_text(card_with("5/5")) == 5.0


def test_reads_a_fractional_score():
    assert RawReview._rating_from_text(card_with("4.5/5")) == 4.5
    assert RawReview._rating_from_text(card_with("4,5/5")) == 4.5


@pytest.mark.parametrize("noise", [
    "41 reviews",          # the author's own review count
    "6 photos",            # and photo count
    "Local Guide",
    "3 years ago on Google",
    "97",
    "1/10",                # a score, but not out of five
    "6/5",                 # out of range
])
def test_does_not_mistake_other_numbers_for_the_score(noise):
    assert RawReview._rating_from_text(card_with(noise)) == 0.0


def test_picks_the_score_out_of_a_realistic_card():
    card = card_with("Ece Kahraman", "Local Guide · 41 reviews · 6 photos",
                     "5/5", "3 years ago on Google", "Kaleiçinin tam merkezinde")
    assert RawReview._rating_from_text(card) == 5.0


def test_a_card_without_a_text_score_stays_zero():
    """Ordinary places label the rating; the text path must not invent one."""
    assert RawReview._rating_from_text(card_with("Ece", "3 years ago")) == 0.0


SOURCE_RE = re.compile(r"\bago\s+on\s+(.+)$", re.IGNORECASE)


@pytest.mark.parametrize("date_text,expected", [
    ("3 years ago on Google", "Google"),
    ("7 years ago on Tripadvisor", "Tripadvisor"),
    ("a month ago on Booking.com", "Booking.com"),
    ("2 weeks ago", None),
    ("", None),
])
def test_source_is_read_from_the_date_line(date_text, expected):
    m = SOURCE_RE.search(date_text)
    assert (m.group(1) if m else None) == expected


@pytest.mark.parametrize("date_text", [
    "3 years ago on Google", "7 years ago on Tripadvisor", "2 weeks ago",
])
def test_the_relative_date_still_parses_with_a_source_suffix(date_text):
    """The source suffix must not cost us the date it is attached to."""
    from workers.engine.utils import parse_date_to_iso
    assert parse_date_to_iso(date_text), f"{date_text!r} produced no date"


def test_source_defaults_to_google():
    """A review with no source named is Google's own — that is the page."""
    assert RawReview().source == "Google"
