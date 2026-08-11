"""
Place details extraction from a Google Maps place page.

Extracts business information beyond reviews: general info (name, category,
rating, review count, address, phone, website, plus code), opening hours
(with reliable absence detection), price range (summary + per-person report
count + histogram buckets), and the dynamic About-tab attribute sections.

Design principles (mirrors modules/scraper.py):
- DOM-based extraction via the already-authenticated Selenium driver.
- Language-independent structural anchors wherever possible:
  * `data-item-id` attributes (address / authority / phone:tel:* / oloc)
  * `role` attributes and stable table classes (eK4R0e hours, rqRH4d histogram)
  * negative About attributes are marked structurally (XJynsc / OazX1c classes),
    not by localized text
- Every section is extracted independently; one failure never aborts the rest.

Must be called AFTER navigate_to_place() has resolved the page (the driver
sitting on the place Overview tab). Clicking the About tab is done internally
and the tab bar is left intact for the subsequent reviews-tab click.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("scraper")

# Currency detection for price fields (symbol or common ISO codes)
_CURRENCY_RE = re.compile(r"[₺$€£¥₹]|\b(?:TRY|TL|USD|EUR|GBP)\b")

# Localized day name -> ISO weekday index (1 = Monday). Extend as needed;
# unknown names keep day_index=None while the raw label is always preserved.
_DAY_INDEX = {
    # English
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 7,
    # Turkish
    "pazartesi": 1, "salı": 2, "sali": 2, "çarşamba": 3, "carsamba": 3,
    "perşembe": 4, "persembe": 4, "cuma": 5, "cumartesi": 6, "pazar": 7,
}


def _digits(text: str) -> Optional[int]:
    """Extract an integer from localized number text ('1.635', '1,635')."""
    if not text:
        return None
    d = re.sub(r"\D", "", text)
    return int(d) if d else None


# Private-use-area glyphs (google-symbols icon ligatures leak into innerText)
_PUA_RE = re.compile("[\ue000-\uf8ff\ufe0e\ufe0f]")


def _clean_text(text: Optional[str]) -> Optional[str]:
    """Strip icon-font ligatures and collapse whitespace/newlines."""
    if not text:
        return None
    cleaned = " ".join(_PUA_RE.sub("", text).split())
    return cleaned or None


def _parse_rating(text: str) -> Optional[float]:
    """Parse a localized rating like '3,4' or '3.4'."""
    if not text:
        return None
    m = re.search(r"\d+[.,]?\d*", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _js(driver, script: str, default=None):
    """execute_script with failure isolation."""
    try:
        return driver.execute_script(script)
    except Exception as e:
        log.debug(f"place_details JS snippet failed: {e}")
        return default


def extract_header(driver) -> Dict[str, Any]:
    """Name, rating, total review count, price summary, category."""
    data = _js(driver, """
        const r = {};
        const h1 = document.querySelector('h1');
        if (h1) r.name = h1.innerText.trim();
        const hdr = document.querySelector('div.LBgpqf, div.skqShb, div.tAiQdd') || document;
        const f7 = hdr.querySelector('div.F7nice');
        if (f7) {
            const rate = f7.querySelector('span[aria-hidden="true"]');
            if (rate) r.rating_text = rate.innerText.trim();
            // star span has role="img"; the review-count span does not
            const cnt = Array.from(f7.querySelectorAll('span[aria-label]'))
                .find(s => s.getAttribute('role') !== 'img');
            if (cnt) r.review_count_aria = cnt.getAttribute('aria-label');
        }
        r.header_text = (hdr.innerText || '').slice(0, 300);
        // price summary: short currency span in the header block
        const priceSpan = Array.from(hdr.querySelectorAll('span'))
            .find(s => s.children.length === 0 && /[₺$€£¥₹]/.test(s.innerText) && s.innerText.trim().length < 15);
        if (priceSpan) r.price_summary = priceSpan.innerText.trim();
        const cats = Array.from(hdr.querySelectorAll('button[jsaction*="category"]'))
            .map(b => b.innerText.trim()).filter(Boolean);
        if (cats.length) r.categories = cats;
        return r;
    """, {}) or {}

    price_summary = (data.get("price_summary") or "").lstrip("·").strip() or None
    categories = data.get("categories") or []
    review_count = _digits(data.get("review_count_aria", ""))
    if review_count is None and data.get("rating_text"):
        # Fallback for the "3,4 (1.635)" header, where the count follows the
        # rating. It only applies when a rating was found, and the parentheses
        # must follow that number — a bare "(...)" anywhere in the header is
        # as likely to be part of the name: "👑 Çiğ Köfte(2002) 👑" was read
        # as 2002 reviews, and the place then failed forever for delivering
        # none of them.
        m = re.search(r"[\d.,]+\s*\(([\d.,\s]+)\)", data.get("header_text") or "")
        if m:
            review_count = _digits(m.group(1))
    return {
        "name": _clean_text(data.get("name")),
        "rating": _parse_rating(data.get("rating_text", "")),
        "review_count": review_count,
        "price_summary": _clean_text(price_summary),
        "category": _clean_text(categories[0]) if categories else None,
        "categories": [c for c in (_clean_text(c) for c in categories) if c],
    }


def extract_contact_info(driver) -> Dict[str, Any]:
    """Address, website, phone, plus code + any other data-item-id entries."""
    items = _js(driver, """
        return Array.from(document.querySelectorAll('[data-item-id]')).map(e => ({
            id: e.getAttribute('data-item-id'),
            tag: e.tagName,
            aria: e.getAttribute('aria-label'),
            href: e.getAttribute('href'),
            text: (e.innerText || '').trim()
        }));
    """, []) or []

    info: Dict[str, Any] = {
        "address": None, "website": None, "website_text": None,
        "phone": None, "phone_raw": None, "plus_code": None,
        "extra_items": {},
    }
    for it in items:
        iid = it.get("id") or ""
        text = _clean_text(it.get("text"))
        if iid == "address":
            info["address"] = text
        elif iid == "authority":
            info["website"] = it.get("href")
            info["website_text"] = text
        elif iid.startswith("phone:tel:"):
            info["phone"] = text
            info["phone_raw"] = iid.split("phone:tel:", 1)[1] or None
        elif iid == "oloc":
            info["plus_code"] = text
        elif iid and not iid.isdigit() and iid not in ("oh",):
            # dynamic catch-all (menu links, reservations, etc.)
            info["extra_items"][iid] = {
                "text": text, "aria": it.get("aria"), "href": it.get("href"),
            }
    return info


def extract_service_options(driver) -> List[str]:
    """Top-of-page service badges ('Dine-in', 'No delivery', ...) as raw arias."""
    return _js(driver, """
        return Array.from(document.querySelectorAll('div.LTs0Rc[aria-label], span.LTs0Rc[aria-label]'))
            .map(e => e.getAttribute('aria-label')).filter(Boolean);
    """, []) or []


def extract_description(driver) -> Optional[str]:
    """Editorial/business description blurb on the Overview tab, if present."""
    desc = _js(driver, """
        const d = document.querySelector('div.PYvSYb, div.WeS02d');
        return d ? d.innerText.trim() : null;
    """)
    return desc or None


def extract_opening_hours(driver) -> Dict[str, Any]:
    """
    Weekly hours table + live status line.

    The collapsed widget renders only today's row, so the summary row is
    clicked first to expand all seven days. `available` is the definitive
    signal: False means the business genuinely publishes no hours.
    """
    result: Dict[str, Any] = {"available": False, "status_text": None, "days": []}

    # Expand the hours widget (no-op when there is none)
    clicked = _js(driver, """
        const el = document.querySelector('div.OMl5r, button[data-item-id="oh"], div[jsaction*="openhours"]');
        if (el) { el.click(); return true; }
        return false;
    """, False)
    if clicked:
        time.sleep(1.5)

    data = _js(driver, """
        const r = {};
        const s = document.querySelector('div.OMl5r, div.o0Svhf');
        if (s) r.status = s.innerText.trim();
        const t = document.querySelector('table.eK4R0e');
        if (t) {
            r.rows = Array.from(t.querySelectorAll('tr')).map(tr => {
                const dayTd = tr.querySelector('td.ylH6lf') || tr.querySelector('td');
                const hrsTd = tr.querySelector('td.mxowUb') || tr.querySelectorAll('td')[1];
                return {
                    day: dayTd ? dayTd.innerText.trim() : null,
                    hours: hrsTd ? hrsTd.innerText.trim() : null,
                    aria: hrsTd ? hrsTd.getAttribute('aria-label') : null,
                };
            }).filter(x => x.day || x.hours);
        }
        return r;
    """, {}) or {}

    status = data.get("status")
    if status:
        # first meaningful line, e.g. "Kapalı · Açılış zamanı: 14:00"
        lines = [c for c in (_clean_text(ln) for ln in status.splitlines()) if c]
        result["status_text"] = " · ".join(lines[:2]) if lines else None

    for row in data.get("rows") or []:
        day_raw = _clean_text(row.get("day")) or ""
        hours_txt = _clean_text((row.get("hours") or "").replace("\n", ", "))
        result["days"].append({
            "day": day_raw or None,
            "day_index": _DAY_INDEX.get(day_raw.lower()) if day_raw else None,
            "hours": hours_txt,
        })

    result["available"] = bool(result["days"]) or bool(result["status_text"])
    return result


def extract_price(driver) -> Dict[str, Any]:
    """
    Price range block: per-person summary, reporter count, and the
    distribution histogram that backs it (bucket label + percent).
    """
    result: Dict[str, Any] = {
        "available": False, "per_person_text": None,
        "reported_by": None, "histogram": [],
    }

    row = _js(driver, """
        // the expandable price row (aria-expanded + currency in text)
        const cands = Array.from(document.querySelectorAll('[aria-expanded]'))
            .filter(e => /[₺$€£¥₹]/.test(e.innerText || ''));
        if (!cands.length) return null;
        const el = cands[0];
        const r = { text: (el.innerText || '').trim(),
                    aria: el.getAttribute('aria-label'),
                    controls: el.getAttribute('aria-controls'),
                    expanded: el.getAttribute('aria-expanded') };
        if (r.expanded === 'false') el.click();
        return r;
    """)
    if not row:
        return result

    result["available"] = True
    lines = [c for c in (_clean_text(ln) for ln in (row.get("text") or "").splitlines()) if c]
    if lines:
        result["per_person_text"] = lines[0]
    if len(lines) > 1:
        result["reported_by"] = _digits(lines[1])

    if row.get("expanded") == "false":
        time.sleep(1.5)

    controls = row.get("controls")
    hist = _js(driver, f"""
        const panel = document.getElementById({controls!r}) || document;
        const t = panel.querySelector('table.rqRH4d') ||
                  Array.from(panel.querySelectorAll('table')).find(x => x.querySelector('span[role="img"]'));
        if (!t) return [];
        return Array.from(t.querySelectorAll('tr')).map(tr => {{
            const label = tr.querySelector('td') ? tr.querySelector('td').innerText.trim() : null;
            const bar = tr.querySelector('span[role="img"]');
            return {{ label: label, percent_aria: bar ? bar.getAttribute('aria-label') : null }};
        }}).filter(x => x.label);
    """, []) if controls else []

    for h in hist or []:
        result["histogram"].append({
            "label": _clean_text(h.get("label")),
            "percent": _digits(h.get("percent_aria") or ""),
        })
    return result


def extract_about(driver) -> Dict[str, Any]:
    """
    About tab: every attribute section, fully dynamic.

    Tab discovery is structural: candidate tabs are clicked (last first —
    About is conventionally the final tab) until `div.iP2t7d` sections
    render. Negative attributes are detected via the structural
    XJynsc/OazX1c marker classes, not localized 'no/yok' strings.
    """
    result: Dict[str, Any] = {"available": False, "sections": []}

    try:
        from selenium.webdriver.common.by import By
        tabs = driver.find_elements(By.CSS_SELECTOR, 'button[role="tab"]')
    except Exception as e:
        log.debug(f"About: tab lookup failed: {e}")
        return result
    if not tabs:
        return result

    # last tab first, skip the currently selected one
    candidates = [t for t in reversed(tabs)
                  if (t.get_attribute("aria-selected") or "") != "true"]
    for tab in candidates:
        try:
            driver.execute_script("arguments[0].click();", tab)
        except Exception:
            continue
        time.sleep(2)
        sections = _js(driver, """
            return Array.from(document.querySelectorAll('div.iP2t7d')).map(s => ({
                heading: s.querySelector('h2') ? s.querySelector('h2').innerText.trim() : null,
                items: Array.from(s.querySelectorAll('li')).map(li => {
                    const wrap = li.querySelector('div');
                    const icon = li.querySelector('span[aria-hidden="true"]');
                    const lbl = li.querySelector('span[aria-label]');
                    const negative = (wrap && wrap.className.indexOf('XJynsc') !== -1) ||
                                     (icon && icon.className.indexOf('OazX1c') !== -1);
                    return {
                        label: (li.innerText || '').trim(),
                        detail: lbl ? lbl.getAttribute('aria-label') : null,
                        enabled: !negative,
                    };
                }).filter(x => x.label),
            })).filter(s => s.heading && s.items.length);
        """, [])
        if sections:
            for sec in sections:
                sec["heading"] = _clean_text(sec.get("heading"))
                for item in sec.get("items") or []:
                    item["label"] = _clean_text(item.get("label"))
            result["available"] = True
            result["sections"] = sections
            break

    return result


_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday",
             "Thursday", "Friday", "Saturday")


def extract_popular_times(driver) -> Dict[str, Any]:
    """
    "Popular times": hour-by-hour busyness for each day of the week.

    All seven days sit in the DOM at once (one div.g2BVhd per day, Sunday
    first; only the current day is visible), so this is a pure read — no
    clicking, no navigation. Each bar carries its own aria-label:

        "24% busy at 12 PM."                      -> hour 12, 24%
        "Currently 23% busy, usually 20% busy."   -> the live hour

    The live bar omits its hour, so it is filled in from its neighbours.
    Hours are 0-23; the range covered varies by place (many start at 6 AM).
    """
    result: Dict[str, Any] = {"available": False, "days": [],
                              "current_percent": None}

    raw = _js(driver, """
        const w = document.querySelector('div[aria-label^="Popular times"]');
        if (!w) return null;
        return Array.from(w.querySelectorAll('div.g2BVhd')).map(day =>
            Array.from(day.querySelectorAll('div[aria-label]'))
                 .map(b => b.getAttribute('aria-label'))
                 .filter(Boolean));
    """)
    if not raw:
        return result

    for day_index, labels in enumerate(raw[:7]):
        hours: List[Dict[str, Any]] = []
        live_slots = []
        for label in labels:
            live = re.search(r"[Cc]urrently\s+(\d+)%.*?(\d+)%", label)
            if live:
                result["current_percent"] = int(live.group(1))
                hours.append({"hour": None, "percent": int(live.group(2)),
                              "live": True})
                live_slots.append(len(hours) - 1)
                continue
            m = re.search(r"(\d+)%.*?\b(\d{1,2})\s*(AM|PM)\b", label, re.I)
            if not m:
                continue
            hour = int(m.group(2)) % 12
            if m.group(3).upper() == "PM":
                hour += 12
            hours.append({"hour": hour, "percent": int(m.group(1)),
                          "live": False})

        # the live bar has no hour of its own: take previous + 1 (or next - 1)
        for i in live_slots:
            prev_h = hours[i - 1]["hour"] if i > 0 else None
            next_h = hours[i + 1]["hour"] if i + 1 < len(hours) else None
            if prev_h is not None:
                hours[i]["hour"] = (prev_h + 1) % 24
            elif next_h is not None:
                hours[i]["hour"] = (next_h - 1) % 24

        hours = [h for h in hours if h["hour"] is not None]
        if not hours:
            continue
        peak = max(hours, key=lambda h: h["percent"])
        result["days"].append({
            "day": _WEEKDAYS[day_index],
            "day_index": day_index,
            "hours": hours,
            "peak_hour": peak["hour"] if peak["percent"] else None,
            "peak_percent": peak["percent"] or None,
        })

    result["available"] = bool(result["days"])
    return result


# Localized aria-labels of the owner-photos category card in the
# "Photos & videos" carousel. With the default language=en config this is
# always "By owner"; other locales are best-effort fallbacks.
_OWNER_PHOTO_LABELS = (
    "By owner",           # en
    "Sahibinden",         # tr
    "Vom Inhaber",        # de
    "Par le propriétaire",  # fr
    "Del propietario",    # es
    "Dal proprietario",   # it
)


def _photo_large_url(url: str) -> str:
    """Rewrite a googleusercontent thumbnail URL to a large variant."""
    if "googleusercontent" not in url or "=" not in url:
        return url
    return url.rsplit("=", 1)[0] + "=s1600-k-no"


def extract_owner_photos(driver, limit: int = 60) -> Dict[str, Any]:
    """
    Photos uploaded by the business owner ("By owner" category in the
    Photos & videos carousel).

    Opens the category gallery overlay, scrolls until `limit` tiles are
    loaded (or the grid stops growing), collects the tile image URLs, and
    navigates Back to the place page — leaving the tab bar intact for the
    subsequent Reviews-tab click.

    available=False distinguishes "place has no By owner category" from
    an empty result caused by an extraction failure (available stays True).
    """
    result: Dict[str, Any] = {"available": False, "count": 0, "photos": []}

    labels_js = json.dumps(list(_OWNER_PHOTO_LABELS))
    clicked = _js(driver, f"""
        const labels = {labels_js};
        const btn = Array.from(document.querySelectorAll('button[aria-label]'))
            .find(b => labels.includes(b.getAttribute('aria-label')));
        if (!btn) return null;
        btn.click();
        return btn.getAttribute('aria-label');
    """)
    if not clicked:
        return result

    result["available"] = True
    time.sleep(3)

    # Scroll the gallery grid until enough tiles load or growth stops
    prev_count, stagnant = 0, 0
    for _ in range(20):
        count = _js(driver, """
            const tiles = document.querySelectorAll('a[data-photo-index], div[data-photo-index]');
            if (tiles.length) tiles[tiles.length - 1].scrollIntoView({block: 'end'});
            return tiles.length;
        """, 0) or 0
        if count >= limit:
            break
        stagnant = stagnant + 1 if count == prev_count else 0
        if stagnant >= 2:
            break
        prev_count = count
        time.sleep(1.5)

    tiles = _js(driver, """
        return Array.from(document.querySelectorAll('a[data-photo-index], div[data-photo-index]'))
            .map(t => {
                const bg = t.querySelector('div[style*="background-image"]');
                let url = null;
                if (bg) {
                    const m = (bg.getAttribute('style') || '').match(/url\\("?([^")]+)"?\\)/);
                    if (m) url = m[1];
                }
                if (!url) {
                    const img = t.querySelector('img');
                    if (img) url = img.src;
                }
                return { index: t.getAttribute('data-photo-index'), url: url };
            }).filter(t => t.url && t.url.includes('googleusercontent'));
    """, []) or []

    for t in tiles[:limit]:
        url = t.get("url") or ""
        try:
            idx = int(t.get("index"))
        except (TypeError, ValueError):
            idx = None
        result["photos"].append({
            "index": idx,
            "url": url,
            "url_large": _photo_large_url(url),
        })
    result["count"] = len(result["photos"])

    # Navigate back to the place page (tab bar must survive for reviews)
    _js(driver, """
        const b = Array.from(document.querySelectorAll('button[aria-label]'))
            .find(x => /^(back|geri|zurück|retour|atrás|indietro)$/i
                .test((x.getAttribute('aria-label') || '').trim()));
        if (b) b.click();
    """)
    time.sleep(2.5)
    return result


def extract_review_count_on_reviews_tab(driver) -> Optional[int]:
    """
    Total review count from the Reviews-tab summary block ("1.635 yorum").

    Used as a backfill when the Overview header rendered a promo callout in
    place of the review-count link (a known nondeterministic Maps variant).
    """
    text = _js(driver, """
        // summary block next to the big rating number on the Reviews tab
        const el = document.querySelector('div.jANrlb div.fontBodySmall') ||
                   document.querySelector('div.PPCwl div.fontBodySmall') ||
                   document.querySelector('button[jsaction*="moreReviews"]');
        return el ? (el.innerText || el.getAttribute('aria-label') || '') : null;
    """)
    return _digits(text or "")


def extract_place_details(driver, include_about: bool = True,
                          include_photos: bool = True,
                          photos_limit: int = 60) -> Dict[str, Any]:
    """
    Run all extractors against the current place page and return one document.

    Extraction order matters: overview-scoped sections run first; the owner
    photos gallery (overlay + Back) runs next while the Overview carousel is
    still present; the About tab click comes last because it navigates away
    from Overview.
    """
    details: Dict[str, Any] = {}

    for key, fn in (
        ("header", extract_header),
        ("contact", extract_contact_info),
        ("service_options", extract_service_options),
        ("description", extract_description),
        ("opening_hours", extract_opening_hours),
        ("price", extract_price),
        ("popular_times", extract_popular_times),
    ):
        try:
            details[key] = fn(driver)
        except Exception as e:
            log.warning(f"place_details: '{key}' extraction failed: {e}")
            details[key] = None

    # The owner-photo gallery lives in the Overview carousel, so it has to run
    # before the About tab click navigates away. Callers that want the gallery
    # to run dead last (see scraper._extract_and_store_place_details) pass
    # include_photos=False and call extract_owner_photos themselves after
    # reloading the place page.
    if include_photos:
        try:
            details["owner_photos"] = extract_owner_photos(driver, limit=photos_limit)
        except Exception as e:
            log.warning(f"place_details: 'owner_photos' extraction failed: {e}")
            details["owner_photos"] = None

    if include_about:
        try:
            details["about"] = extract_about(driver)
        except Exception as e:
            log.warning(f"place_details: 'about' extraction failed: {e}")
            details["about"] = None

    # flatten header/contact into top level for convenient consumption
    flat: Dict[str, Any] = {}
    header = details.pop("header", None) or {}
    contact = details.pop("contact", None) or {}
    flat.update(header)
    flat.update(contact)
    flat["service_options"] = details.pop("service_options", None) or []
    flat["description"] = details.pop("description", None)
    flat["opening_hours"] = details.get("opening_hours")
    flat["price"] = details.get("price")
    flat["popular_times"] = details.get("popular_times")
    flat["owner_photos"] = details.get("owner_photos")
    flat["about"] = details.get("about")
    flat["scraped_at"] = datetime.now(timezone.utc).isoformat()
    try:
        flat["source_url"] = driver.current_url
    except Exception:
        flat["source_url"] = None
    return flat
