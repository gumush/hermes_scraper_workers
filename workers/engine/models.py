"""
Data models for Google Maps Reviews Scraper.
"""
import logging
import re
from dataclasses import dataclass, field

from selenium.webdriver.remote.webelement import WebElement

from workers.engine.sub_rating_labels import canonicalize_category
from workers.engine.utils import (try_find, first_text, first_attr, safe_int, detect_lang, parse_date_to_iso)

log = logging.getLogger("scraper")


@dataclass
class RawReview:
    """
    Data class representing a raw review extracted from Google Maps.
    """
    id: str = ""
    author: str = ""
    rating: float = 0.0
    date: str = ""
    lang: str = "und"
    text: str = ""
    likes: int = 0
    photos: list[str] = field(default_factory=list)
    profile: str = ""
    avatar: str = ""
    owner_date: str = ""
    owner_text: str = ""
    review_date: str = ""
    # Where the review came from. Hotel pages list Tripadvisor, Booking and
    # others next to Google's own, so "a review on this page" is no longer the
    # same thing as "a Google review" — a measured page had 7 Tripadvisor to 3
    # Google. Recording it keeps the distinction available downstream instead
    # of silently blending the two.
    source: str = "Google"
    sub_ratings: dict = field(default_factory=dict)
    translations: dict = field(default_factory=dict)

    # CSS selector candidates — tried in order, first match wins.
    MORE_BTN = (
        "button.kyuRq",
        'button[jsaction*="expandReview"]',
        'button[aria-expanded="false"][jsaction*="review" i]',
    )
    LIKE_BTN = 'button[jsaction*="toggleThumbsUp" i]'
    PHOTO_BTN_SELECTORS = (
        "button.Tya61d",
        'button[aria-label*="Photo" i][style*="url"]',
        'button[data-photo-index]',
    )
    OWNER_RESP_SELECTORS = (
        "div.CDe7pd",
        'div[class*="owner" i]',
    )
    OWNER_DATE_SELECTORS = (
        "span.DZSIDd",
        'span[class*="ownerdate" i]',
    )
    OWNER_TEXT_SELECTORS = (
        "div.wiI7pd",
        'div[class*="ownerresp" i]',
    )
    TEXT_SELECTORS = (
        'span[jsname="bN97Pc"]',
        'span[jsname="fbQN7e"]',
        'div.MyEned span.wiI7pd',
    )
    RATING_SELECTORS = (
        'span[role="img"][aria-label]',
        'span[class*="kvMYJc" i]',
    )
    # Hotels render the score as plain text ("5/5") in a span with no
    # aria-label and no role, so both selectors above return nothing and every
    # hotel review was stored with rating 0.0 — measured on Odile Konak: 0 of
    # 20 cards matched. Matched on the text rather than the obfuscated class
    # name, which is the part Google keeps stable.
    RATING_TEXT = re.compile(r"^([0-5](?:[.,]\d)?)\s*/\s*5$")
    DATE_SELECTORS = (
        'span[class*="rsqaWe"]',
        'span[class*="xRkPPb" i]',
    )
    SUB_RATING_SELECTORS = (
        'div.PBK6be',
        'div[class*="rating" i][aria-label*="/5" i]',
    )

    @classmethod
    def from_card(cls, card: WebElement) -> "RawReview":
        """Factory method to create a RawReview from a WebElement."""
        for sel in cls.MORE_BTN:
            buttons = try_find(card, sel, all=True)
            if buttons:
                for b in buttons:
                    try:
                        b.click()
                    except Exception:
                        pass
                break

        rid = card.get_attribute("data-review-id") or ""
        author = first_text(card, 'div[class*="d4r55"]')
        profile = first_attr(card, 'button[data-review-id]', "data-href")
        avatar = first_attr(card, 'button[data-review-id] img', "src")

        rating = 0.0
        for sel in cls.RATING_SELECTORS:
            label = first_attr(card, sel, "aria-label")
            if label:
                num = re.search(r"[\d\.]+", label.replace(",", "."))
                if num:
                    try:
                        rating = float(num.group())
                        if 0 < rating <= 5:
                            break
                    except ValueError:
                        continue

        if not rating:
            rating = cls._rating_from_text(card)

        date = ""
        for sel in cls.DATE_SELECTORS:
            date = first_text(card, sel)
            if date:
                break
        # "3 years ago on Google" / "7 years ago on Tripadvisor" — hotel cards
        # name the source in the date line. The relative-date parser searches
        # rather than anchors so it is unaffected, but the source is worth
        # keeping.
        source = "Google"
        if (m := re.search(r"\bago\s+on\s+(.+)$", date, re.IGNORECASE)):
            source = m.group(1).strip()
        review_date = parse_date_to_iso(date)

        text = ""
        for sel in cls.TEXT_SELECTORS:
            text = first_text(card, sel)
            if text:
                break
        lang = detect_lang(text)

        likes = 0
        if (btn := try_find(card, cls.LIKE_BTN)):
            likes = safe_int(btn[0].text or btn[0].get_attribute("aria-label"))

        photos: list[str] = []
        for sel in cls.PHOTO_BTN_SELECTORS:
            found = try_find(card, sel, all=True)
            if not found:
                continue
            for btn in found:
                style = btn.get_attribute("style") or ""
                m = re.search(r'url\(["\']?([^"\')]+)', style)
                if m:
                    url = m.group(1)
                    if url not in photos:
                        photos.append(url)
            if photos:
                break

        owner_date = owner_text = ""
        for sel in cls.OWNER_RESP_SELECTORS:
            box_list = try_find(card, sel)
            if not box_list:
                continue
            box = box_list[0]
            for d_sel in cls.OWNER_DATE_SELECTORS:
                owner_date = first_text(box, d_sel)
                if owner_date:
                    break
            for t_sel in cls.OWNER_TEXT_SELECTORS:
                owner_text = first_text(box, t_sel)
                if owner_text:
                    break
            break

        sub_ratings = cls._extract_sub_ratings(card)

        return cls(
            id=rid,
            author=author,
            rating=rating,
            date=date,
            lang=lang,
            text=text,
            likes=likes,
            photos=photos,
            profile=profile,
            avatar=avatar,
            owner_date=owner_date,
            owner_text=owner_text,
            review_date=review_date,
            source=source,
            sub_ratings=sub_ratings,
        )

    @classmethod
    def _rating_from_text(cls, card: WebElement) -> float:
        """
        Score read from a card that writes it as text instead of labelling it.

        Only leaf spans are considered and only an exact "N/5"; the card also
        carries the author's review count and photo count, and a looser match
        would happily read one of those as the rating.
        """
        for el in (try_find(card, "span", all=True) or []):
            try:
                if el.find_elements("css selector", "*"):
                    continue
                m = cls.RATING_TEXT.match((el.text or "").strip())
            except Exception:  # noqa: BLE001
                continue
            if m:
                try:
                    value = float(m.group(1).replace(",", "."))
                except ValueError:
                    continue
                if 0 < value <= 5:
                    return value
        return 0.0

    @classmethod
    def _extract_sub_ratings(cls, card: WebElement) -> dict:
        """Extract per-category sub-ratings (e.g. Service 5/5, Food 4/5)."""
        result: dict = {}
        for sel in cls.SUB_RATING_SELECTORS:
            blocks = try_find(card, sel, all=True)
            if not blocks:
                continue
            for block in blocks:
                try:
                    label = (block.get_attribute("aria-label") or block.text or "").strip()
                    if not label:
                        continue
                    m = re.match(r"(.+?)[:\s]+(\d)\s*/\s*5", label)
                    if not m:
                        continue
                    raw_cat = m.group(1).strip(" :.").lower()
                    score = int(m.group(2))
                    if score < 0 or score > 5:
                        continue
                    canonical = canonicalize_category(raw_cat)
                    if canonical:
                        result[canonical] = score
                    else:
                        result.setdefault("_other", {})[raw_cat] = score
                except (ValueError, AttributeError):
                    continue
            if result:
                break
        return result
