"""
Selenium scraping logic for Google Maps Reviews.
Uses SeleniumBase UC Mode for enhanced anti-detection and better Chrome version management.
"""

import json
import logging
import os
import platform
import re
import threading
import random
import time
from urllib.parse import quote_plus
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from seleniumbase import Driver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import Chrome
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

from workers.engine.date_filter import DateFilter, EARLY_STOP_CONSECUTIVE
from workers.engine.models import RawReview
from workers.engine.pipeline import PostScrapeRunner
from workers.engine.review_db import ReviewDB
from workers.engine.place_details import (
    extract_owner_photos,
    extract_place_details,
    extract_review_count_on_reviews_tab,
)
from workers.engine.place_id import extract_place_id
from workers.engine.selector_health import SelectorHealth

# Logger
log = logging.getLogger("scraper")

# CSS Selectors
PANE_SEL = 'div[role="main"] div.m6QErb.DxyBCb.kA9KIf.dS8AEf'
CARD_SEL = "div[data-review-id]"
COOKIE_BTN = ('button[aria-label*="Accept" i],'
              'button[jsname="hZCF7e"],'
              'button[data-mdc-dialog-action="accept"]')
SORT_BTN = 'button[aria-label="Sort reviews" i], button[aria-label="Sort" i]'
MENU_ITEMS = 'div[role="menu"] [role="menuitem"], li[role="menuitem"]'

SORT_OPTIONS = {
    "newest": (
        "Newest", "החדשות ביותר", "ใหม่ที่สุด", "最新", "Más recientes", "最近",
        "Mais recentes", "Neueste", "Plus récent", "Più recenti", "Nyeste",
        "Новые", "Nieuwste", "جديد", "Nyeste", "Uusimmat", "Najnowsze",
        "Senaste", "Terbaru", "Yakın zamanlı", "Mới nhất", "नवीनतम"
    ),
    "highest": (
        "Highest rating", "הדירוג הגבוה ביותר", "คะแนนสูงสุด", "最高評価",
        "Calificación más alta", "最高评分", "Melhor avaliação", "Höchste Bewertung",
        "Note la plus élevée", "Valutazione più alta", "Høyeste vurdering",
        "Наивысший рейтинг", "Hoogste waardering", "أعلى تقييم", "Højeste vurdering",
        "Korkein arvostelu", "Najwyższa ocena", "Högsta betyg", "Peringkat tertinggi",
        "En yüksek puan", "Đánh giá cao nhất", "उच्चतम रेटिंग", "Top rating"
    ),
    "lowest": (
        "Lowest rating", "הדירוג הנמוך ביותר", "คะแนนต่ำสุด", "最低評価",
        "Calificación más baja", "最低评分", "Pior avaliação", "Niedrigste Bewertung",
        "Note la plus basse", "Valutazione più bassa", "Laveste vurdering",
        "Наименьший рейтинг", "Laagste waardering", "أقل تقييم", "Laveste vurdering",
        "Alhaisin arvostelu", "Najniższa ocena", "Lägsta betyg", "Peringkat terendah",
        "En düşük puan", "Đánh giá thấp nhất", "निम्नतम रेटिंग", "Worst rating"
    ),
    "relevance": (
        "Most relevant", "רלוונטיות ביותר", "เกี่ยวข้องมากที่สุด", "関連性",
        "Más relevantes", "最相关", "Mais relevantes", "Relevanteste",
        "Plus pertinents", "Più pertinenti", "Mest relevante",
        "Наиболее релевантные", "Meest relevant", "الأكثر صلة", "Mest relevante",
        "Olennaisimmat", "Najbardziej trafne", "Mest relevanta", "Paling relevan",
        "En alakalı", "Liên quan nhất", "सबसे प्रासंगिक", "Relevance"
    )
}

# How long to let Maps redirect a place_id URL to its canonical /maps/place/
# form. Generous on purpose: giving up early drops to the weaker name-search
# path, which is what mixed two businesses up in the first place.
PLACE_URL_TIMEOUT = 20

# Words that identify a sort control. Kept at module level so every path that
# looks for one tests the same thing.
NON_SORT_WORDS = ("back", "next", "previous", "close", "cancel",
                  "חזרה", "סגור", "ปิด")


def _has_any_word(text: str, words) -> bool:
    """
    Word-boundary membership, for lists matched against control labels.

    Maps writes the place's name into its controls: the sort button on
    "Backyard by Olde" is labelled "Sort reviews for Backyard by Olde", and a
    substring check on "back" threw away the one control being looked for.
    """
    low = (text or "").lower()
    for w in words:
        if any(ch.isascii() and ch.isalpha() for ch in w):
            if re.search(rf"(?<![a-zçğıöşü]){re.escape(w)}(?![a-zçğıöşü])", low):
                return True
        elif w in low:
            return True
    return False


def _has_sort_word(text: str) -> bool:
    """
    Does this text name a sort control, as a word rather than a fragment?

    Latin keywords are matched between word boundaries; the CJK and Thai
    entries have no boundaries to match on, so they stay substring checks.
    """
    return _has_any_word(text, SORT_WORDS)


SORT_WORDS = ("sort", "sırala", "סידור", "เรียง", "排序", "trier", "ordenar",
              "sortieren")

# How long to wait for the tab strip before concluding a place has no reviews.
TAB_STRIP_TIMEOUT = 12
REVIEWLESS_CONFIRM = 3   # seconds a "no reviews" reading must hold to count

# Comprehensive multi-language review keywords
REVIEW_WORDS = {
    # English
    "reviews", "review", "ratings", "rating",

    # Hebrew
    "ביקורות", "ביקורת", "ביקורות על", "דירוגים", "דירוג",

    # Thai
    "รีวิว", "บทวิจารณ์", "คะแนน", "ความคิดเห็น",

    # Spanish
    "reseñas", "opiniones", "valoraciones", "críticas", "calificaciones",

    # French
    "avis", "commentaires", "évaluations", "critiques", "notes",

    # German
    "bewertungen", "rezensionen", "beurteilungen", "meinungen", "kritiken",

    # Italian
    "recensioni", "valutazioni", "opinioni", "giudizi", "commenti",

    # Portuguese
    "avaliações", "comentários", "opiniões", "análises", "críticas",

    # Russian
    "отзывы", "рецензии", "обзоры", "оценки", "комментарии",

    # Japanese
    "レビュー", "口コミ", "評価", "批評", "感想",

    # Korean
    "리뷰", "평가", "후기", "댓글", "의견",

    # Chinese (Simplified and Traditional)
    "评论", "評論", "点评", "點評", "评价", "評價", "意见", "意見", "回顾", "回顧",

    # Arabic
    "مراجعات", "تقييمات", "آراء", "تعليقات", "نقد",

    # Hindi
    "समीक्षा", "रिव्यू", "राय", "मूल्यांकन", "प्रतिक्रिया",

    # Turkish
    "yorumlar", "değerlendirmeler", "incelemeler", "görüşler", "puanlar",

    # Dutch
    "beoordelingen", "recensies", "meningen", "opmerkingen", "waarderingen",

    # Polish
    "recenzje", "opinie", "oceny", "komentarze", "uwagi",

    # Vietnamese
    "đánh giá", "nhận xét", "bình luận", "phản hồi", "bài đánh giá",

    # Indonesian
    "ulasan", "tinjauan", "komentar", "penilaian", "pendapat",

    # Swedish
    "recensioner", "betyg", "omdömen", "åsikter", "kommentarer",

    # Norwegian
    "anmeldelser", "vurderinger", "omtaler", "meninger", "tilbakemeldinger",

    # Danish
    "anmeldelser", "bedømmelser", "vurderinger", "meninger", "kommentarer",

    # Finnish
    "arvostelut", "arviot", "kommentit", "mielipiteet", "palautteet",

    # Greek
    "κριτικές", "αξιολογήσεις", "σχόλια", "απόψεις", "βαθμολογίες",

    # Czech
    "recenze", "hodnocení", "názory", "komentáře", "posudky",

    # Romanian
    "recenzii", "evaluări", "opinii", "comentarii", "note",

    # Hungarian
    "vélemények", "értékelések", "kritikák", "hozzászólások", "megjegyzések",

    # Bulgarian
    "отзиви", "ревюта", "мнения", "коментари", "оценки"
}

# Negative-signal keywords — tabs whose presence implies this is NOT the
# reviews tab. Used to penalize false positives (Menu/Overview/Photos etc.
# sometimes sit at data-tab-index="1" when Google reorders tabs).
NON_REVIEW_TAB_WORDS = {
    # English
    "menu", "overview", "about", "photos", "updates", "products", "services",
    "directions", "posts",
    # French
    "aperçu", "à propos", "photos", "produits",
    # German
    "übersicht", "speisekarte", "fotos", "produkte", "über",
    # Spanish
    "menú", "resumen", "fotos", "productos", "acerca",
    # Portuguese
    "menu", "visão geral", "fotos", "produtos", "sobre",
    # Italian
    "menu", "panoramica", "foto", "prodotti",
    # Hebrew
    "תפריט", "תמונות", "סקירה כללית", "מוצרים",
    # Thai
    "เมนู", "ภาพรวม", "รูปภาพ", "สินค้า",
    # Russian
    "меню", "обзор", "фото", "товары",
    # Japanese
    "メニュー", "概要", "写真", "商品",
    # Korean
    "메뉴", "개요", "사진", "상품",
    # Chinese
    "菜单", "菜單", "概览", "概覽", "照片", "产品", "產品",
    # Arabic
    "قائمة الطعام", "نظرة عامة", "صور", "منتجات",
    # Turkish
    "menü", "genel bakış", "fotoğraflar", "ürünler",
    # Polish
    "menu", "omówienie", "zdjęcia", "produkty",
    # Dutch
    "menukaart", "overzicht", "foto's", "producten",
    # Vietnamese
    "thực đơn", "tổng quan", "ảnh", "sản phẩm",
}


def _js_tabs(driver) -> List[Dict[str, Any]]:
    """Every tab on the page as the browser sees it — the click target."""
    return driver.execute_script("""
        return Array.from(document.querySelectorAll('[role="tab"]')).map(t => ({
            text: (t.textContent || '').trim().slice(0, 40),
            aria: t.getAttribute('aria-label'),
            selected: t.getAttribute('aria-selected'),
            index: t.getAttribute('data-tab-index'),
            visible: !!(t.offsetWidth || t.offsetHeight),
        }));
    """) or []


def _js_review_controls(driver) -> Dict[str, Any]:
    """Whatever review-specific machinery is (or is not) on the page."""
    return driver.execute_script("""
        const q = s => document.querySelectorAll(s).length;
        return {
            review_cards: q('div[data-review-id]'),
            sort_buttons: Array.from(document.querySelectorAll('button[aria-label]'))
                .map(b => b.getAttribute('aria-label'))
                .filter(a => /sort|sırala/i.test(a)).slice(0, 5),
            panes: q('div.m6QErb'),
            dialogs: q('[role="dialog"]'),
            iframes: q('iframe'),
            body_head: (document.body.innerText || '').slice(0, 400),
        };
    """) or {}


def _text_contains_any(text: str, words: set) -> bool:
    """Return True if any word in `words` appears in lowercased `text`."""
    if not text:
        return False
    low = text.lower()
    return any(w in low for w in words)


class _DriverSessionLost(Exception):
    """
    Internal signal that the Chrome/WebDriver session has died mid-scrape
    (issue #20 — `InvalidSessionIdException`). Caught by the retry wrapper;
    triggers partial-session flush + fresh-driver retry.
    """
    pass


class _RateLimited(Exception):
    """
    Internal signal that Google served a CAPTCHA / 429 / limited-view page.
    Caught by the retry wrapper; triggers cooldown + partial status.
    """
    pass


class GoogleReviewsScraper:
    """Main scraper class for Google Maps reviews"""

    def __init__(self, config: Dict[str, Any],
                 cancel_event: threading.Event | None = None,
                 progress_cb=None):
        """
        Initialize scraper with configuration.

        `progress_cb`, when given, is called with a small dict whenever the
        scrape moves on or makes headway — see `_report`. It lets a caller
        (the spot worker) surface live progress; it is never required and any
        exception it raises is swallowed.
        """
        self.config = config
        self.scrape_mode = config.get("scrape_mode", "update")
        self.cancel_event = cancel_event or threading.Event()
        db_path = config.get("db_path", "reviews.db")
        self.review_db = ReviewDB(db_path)
        self._selector_health: SelectorHealth | None = None
        self._progress_cb = progress_cb
        # Problems worth a human's attention, collected rather than raised:
        # the scrape may still deliver useful data, but the package should not
        # be mistaken for a complete one. The worker ships the run's log
        # alongside them so a postmortem does not need the VM.
        self.flags: List[Dict[str, Any]] = []

    def capture_failure(self, driver, code: str, **detail) -> Optional[str]:
        """
        Freeze the browser's state at the moment something went wrong.

        Reading a log tells you what the code decided; it does not tell you
        what the page looked like when it decided it. Without the page there
        is only inference — which is how a first fix addressed one cause and
        missed a second one sitting right next to it. So: screenshot, full
        DOM, URL, title, and the tab strip the code was trying to click.

        Returns the folder written, or None if capture itself failed (never
        raises: this runs on a path that is already failing).
        """
        root = self.config.get("diagnostics_dir")
        if not root:
            return None
        try:
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            out = Path(root) / f"{stamp}_{code}"
            out.mkdir(parents=True, exist_ok=True)

            try:
                driver.save_screenshot(str(out / "screen.png"))
            except Exception as e:  # noqa: BLE001
                (out / "screen.error.txt").write_text(str(e), encoding="utf-8")
            try:
                (out / "page.html").write_text(driver.page_source, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                (out / "page.error.txt").write_text(str(e), encoding="utf-8")

            meta: Dict[str, Any] = {"code": code, "detail": detail,
                                    "at": datetime.now(timezone.utc).isoformat()}
            for key, fn in (
                ("url", lambda: driver.current_url),
                ("title", lambda: driver.title),
                ("tabs", lambda: _js_tabs(driver)),
                ("review_controls", lambda: _js_review_controls(driver)),
                ("cookies", lambda: [c.get("name") for c in driver.get_cookies()]),
                ("window", lambda: driver.get_window_size()),
            ):
                try:
                    meta[key] = fn()
                except Exception as e:  # noqa: BLE001
                    meta[key] = f"<error: {e}>"
            (out / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            log.info("Failure capture written: %s", out)
            return str(out)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failure capture itself failed: {e}")
            return None

    def _flag(self, code: str, message: str, **detail) -> None:
        """Record a red flag (and say so in the log)."""
        self.flags.append({"code": code, "message": message, **detail})
        log.error("RED FLAG [%s] %s %s", code, message,
                  detail if detail else "")

    def _report(self, phase: str, **fields) -> None:
        """Publish a progress update; never let reporting break the scrape."""
        if not self._progress_cb:
            return
        try:
            self._progress_cb({"phase": phase, **fields})
        except Exception:  # noqa: BLE001
            log.debug("progress callback failed", exc_info=True)

    def _record_selector(self, selector: str, outcome: str) -> None:
        """Telemetry helper — always safe to call."""
        if self._selector_health is not None:
            self._selector_health.record(selector, outcome)

    @staticmethod
    def _db_review_to_legacy(db_review: Dict[str, Any]) -> Dict[str, Any]:
        """Convert DB review format to legacy format for MongoDB/JSON compat."""
        text = db_review.get("review_text", {})
        description = text if isinstance(text, dict) else {}
        images = db_review.get("user_images", [])
        owner = db_review.get("owner_responses", {})
        sub_ratings = db_review.get("sub_ratings") or {}
        if not isinstance(sub_ratings, dict):
            sub_ratings = {}
        return {
            "review_id": db_review.get("review_id", ""),
            "place_id": db_review.get("place_id", ""),
            "author": db_review.get("author", ""),
            "rating": db_review.get("rating", 0),
            "description": description,
            "likes": db_review.get("likes", 0),
            "user_images": images if isinstance(images, list) else [],
            "author_profile_url": db_review.get("profile_url", ""),
            "profile_picture": db_review.get("profile_picture", ""),
            "owner_responses": owner if isinstance(owner, dict) else {},
            "sub_ratings": sub_ratings,
            "created_date": db_review.get("created_date", ""),
            "review_date": db_review.get("review_date", ""),
            "last_modified_date": db_review.get("last_modified", ""),
        }

    def setup_driver(self, headless: bool):
        """
        Set up and configure Chrome driver using SeleniumBase UC Mode.
        SeleniumBase provides enhanced anti-detection and automatic Chrome/ChromeDriver version management.
        Works in both Docker containers and on regular OS installations (Windows, Mac, Linux).
        """
        # Log platform information for debugging
        log.info(f"Platform: {platform.platform()}")
        log.info(f"Python version: {platform.python_version()}")
        log.info("Using SeleniumBase UC Mode for enhanced anti-detection")

        # Browser UI language (also applied as hl= param during navigation)
        locale_code = self.config.get("language") or None
        if locale_code:
            log.info(f"Browser locale: {locale_code}")

        # Determine if we're running in a container
        in_container = os.environ.get('CHROME_BIN') is not None

        if in_container:
            chrome_binary = os.environ.get('CHROME_BIN')
            log.info(f"Container environment detected")
            log.info(f"Chrome binary: {chrome_binary}")

            # Create driver with custom binary location for containers
            if chrome_binary and os.path.exists(chrome_binary):
                try:
                    driver = Driver(
                        uc=True,
                        headless=headless,
                        binary_location=chrome_binary,
                        page_load_strategy="normal",
                        locale_code=locale_code
                    )
                    log.info("Successfully created SeleniumBase UC driver with custom binary")
                except Exception as e:
                    log.warning(f"Failed to create driver with custom binary: {e}")
                    # Fall back to default
                    driver = Driver(
                        uc=True,
                        headless=headless,
                        page_load_strategy="normal",
                        locale_code=locale_code
                    )
                    log.info("Successfully created SeleniumBase UC driver with defaults")
            else:
                driver = Driver(
                    uc=True,
                    headless=headless,
                    page_load_strategy="normal",
                    locale_code=locale_code
                )
                log.info("Successfully created SeleniumBase UC driver")
        else:
            # Regular OS environment - SeleniumBase handles version matching automatically
            log.info("Creating SeleniumBase UC Mode driver")
            try:
                driver = Driver(
                    uc=True,
                    headless=headless,
                    page_load_strategy="normal",
                    incognito=True,  # Use incognito mode for better stealth
                    locale_code=locale_code
                )
                log.info("Successfully created SeleniumBase UC driver")
            except Exception as e:
                log.error(f"Failed to create SeleniumBase driver: {e}")
                raise

        # Set page load timeout to avoid hanging
        driver.set_page_load_timeout(30)

        # Set window size
        driver.set_window_size(1400, 900)

        # Add additional stealth settings and Google Maps login-state bypass
        try:
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                '''
            })
            log.info("Additional stealth settings applied")
        except Exception as e:
            log.debug(f"Could not apply additional stealth settings: {e}")

        log.info("SeleniumBase UC driver setup completed successfully")
        return driver

    def dismiss_cookies(self, driver: Chrome):
        """
        Dismiss cookie consent dialogs if present.
        Handles stale element references by re-finding elements if needed.
        """
        try:
            # Use WebDriverWait with expected_conditions to handle stale elements
            WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COOKIE_BTN))
            )
            log.info("Cookie consent dialog found, attempting to dismiss")

            # Get elements again after waiting to avoid stale references
            elements = driver.find_elements(By.CSS_SELECTOR, COOKIE_BTN)
            for elem in elements:
                try:
                    if elem.is_displayed():
                        elem.click()
                        log.info("Cookie dialog dismissed")
                        return True
                except Exception as e:
                    log.debug(f"Error clicking cookie button: {e}")
                    continue
        except TimeoutException:
            # This is expected if no cookie dialog is present
            log.debug("No cookie consent dialog detected")
        except Exception as e:
            log.debug(f"Error handling cookie dialog: {e}")

        return False

    def _extract_place_name(self, driver: Chrome, url: str) -> str:
        """
        Extract the place name from a Google Maps URL.
        Tries URL decoding first, then falls back to loading the page.
        """
        import urllib.parse

        # Try to extract from URL path (e.g. /maps/place/PLACE+NAME/...).
        # `/maps/place/?q=place_id:ChIJ...` has no name in the path \u2014 the
        # segment is a query string. Searching Maps for that literal text
        # returns whatever is near the caller instead of the requested place,
        # so treat it as "no name" and let the title lookup below decide.
        match = re.search(r'/maps/place/([^/@?]+)', url)
        if match:
            name = urllib.parse.unquote(match.group(1))
            # Remove Unicode control characters
            name = re.sub(r'[\u200e\u200f\u202a-\u202e]', '', name)
            if len(name) > 2 and "=" not in name:
                log.info(f"Extracted place name from URL: '{name}'")
                return name

        # If the URL is a shortened URL or we couldn't parse the name,
        # load it briefly to get the title
        try:
            driver.get(self._with_hl(url))
            time.sleep(4)
            # Get the page title - usually "Place Name - Google Maps"
            title = driver.title or ""
            name = title.replace(" - Google Maps", "").strip()
            name = re.sub(r'[\u200e\u200f\u202a-\u202e]', '', name)
            if name:
                log.info(f"Extracted place name from page title: '{name}'")
                return name
        except Exception as e:
            log.debug(f"Could not extract place name from page: {e}")

        return ""

    def _with_hl(self, url: str) -> str:
        """
        Append the configured UI-language param (hl=) to a Google URL.
        Google Maps localizes content by hl, not just browser locale.
        """
        lang = self.config.get("language")
        if not lang or "hl=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}hl={lang}"

    def _extract_place_coords(self, url: str) -> tuple:
        """Extract lat/lng coordinates from a Google Maps URL."""
        match = re.search(r'@(-?[\d.]+),(-?[\d.]+)', url)
        if match:
            return match.group(1), match.group(2)
        match = re.search(r'!3d(-?[\d.]+)!4d(-?[\d.]+)', url)
        if match:
            return match.group(1), match.group(2)
        return None, None

    @staticmethod
    def _requested_place_id(url: str) -> Optional[str]:
        """The Google Place ID the caller asked for, if the URL carries one."""
        m = re.search(r"place_id[:=]([A-Za-z0-9_-]{10,})", url or "")
        return m.group(1) if m else None

    # --- staged verification -------------------------------------------------
    #
    # Every step that changes what the browser is looking at is followed by a
    # check that it actually happened. Google Maps clicks are fire-and-forget:
    # a click can "succeed" while the page stays put, and the next step then
    # reads the wrong pane. Waiting longer does not fix that — only asking the
    # page what it is showing does.

    @staticmethod
    def _wait_until(check, timeout: float, interval: float = 1.0) -> bool:
        """Poll `check` until it is true or the deadline passes."""
        deadline = time.time() + timeout
        while True:
            try:
                if check():
                    return True
            except WebDriverException:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    def _on_reviews_tab(self, driver: Chrome) -> bool:
        """
        Is the reviews list actually on screen?

        Only review-specific evidence counts. The generic pane class
        (`m6QErb…`) also matches the Overview pane, which is why a scrape
        could sit on Overview reporting "found reviews pane" and then scroll
        an empty list to the end of its patience.
        """
        try:
            if driver.find_elements(By.CSS_SELECTOR, "div[data-review-id]"):
                return True
            for sel in ('button[aria-label*="Sort review" i]',
                        'button[aria-label*="Yorumları sırala" i]'):
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            for tab in driver.find_elements(
                    By.CSS_SELECTOR, '[role="tab"][aria-selected="true"]'):
                label = (tab.get_attribute("aria-label") or "") + " " + (tab.text or "")
                if _text_contains_any(label.lower(), REVIEW_WORDS):
                    return True
        except WebDriverException:
            return False
        return False

    @staticmethod
    def _has_own_rating(driver: Chrome) -> bool:
        """Does the place's own header carry a rating block?"""
        try:
            return bool(driver.execute_script("""
                const hdr = document.querySelector(
                    'div.LBgpqf, div.skqShb, div.tAiQdd');
                if (!hdr) return false;
                const f7 = hdr.querySelector('div.F7nice');
                if (!f7) return false;
                // The block is present but empty on places with no reviews,
                // so its existence proves nothing — a number in it does.
                return /\\d/.test((f7.innerText || '').trim());
            """))
        except Exception:  # noqa: BLE001
            return False

    # Two ordinary words to search for. Kept dull and unrelated to the job:
    # the point is a plausible visit, not a useful one.
    WARMUP_WORDS = (
        "hava", "kitap", "kahve", "tarif", "otobüs", "sözlük", "harita",
        "weather", "recipe", "museum", "train", "dictionary", "poster",
        "market", "concert", "flight", "bakery", "island", "festival",
    )

    def _extended_warmup(self, driver: Chrome) -> None:
        """
        Arrive at Maps having already been somewhere.

        Two places served their rating without a review count, no Reviews tab
        and no review cards — from thirteen cloud addresses and from a home
        connection alike, while the same URL in an ordinary browser on that
        same connection carried the reviews. The address was not the
        difference, so the remaining one is how the session looks: no
        history, no cookies, straight to a place page.

        So: a search for two unrelated words, one result opened, then back.
        Best effort throughout — a warmup that fails must not cost the scrape
        it was meant to help.
        """
        try:
            words = " ".join(random.sample(self.WARMUP_WORDS, 2))
            self._report("warmup", step=f"arama: {words}")
            log.info("Extended warmup: searching %r", words)
            driver.get(self._with_hl("https://www.google.com/search?q="
                                     + quote_plus(words)))
            self.dismiss_cookies(driver)
            time.sleep(random.uniform(1.5, 3.0))

            links = [a for a in driver.find_elements(By.CSS_SELECTOR, "a[href^='http']")
                     if (a.get_attribute("href") or "").startswith("http")
                     and "google." not in (a.get_attribute("href") or "")]
            if links:
                target = random.choice(links[:8])
                href = target.get_attribute("href")
                log.info("Extended warmup: opening %s", (href or "")[:80])
                self._report("warmup", step="sonuç açılıyor")
                driver.get(href)
                time.sleep(random.uniform(2.0, 4.0))
            else:
                log.info("Extended warmup: no result link found, search only")
            self._report("warmup", step="bitti")
        except Exception as e:  # noqa: BLE001
            # Never fatal: this is preparation, not the work.
            log.warning("Extended warmup skipped: %s", str(e)[:120])

    @staticmethod
    def _rating_block_text(driver: Chrome) -> str:
        """Raw text of the place's own rating block, or "" if there is none."""
        try:
            return driver.execute_script("""
                const hdr = document.querySelector(
                    'div.LBgpqf, div.skqShb, div.tAiQdd');
                const f7 = hdr && hdr.querySelector('div.F7nice');
                return f7 ? (f7.innerText || '').trim() : '';
            """) or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _reviews_withheld(rating_text: str) -> bool:
        """
        A rating with no count beside it: the reviews exist but were not sent.

        Normally the block reads "4.6(16)" — score and how many. A business
        with nothing to show has no score at all. Score without a count is a
        third state, and it is the one Google serves to some clients: twenty
        captures of two places had it, every one with zero review cards and no
        Reviews tab, while the same places from a residential address carried
        the count, the tab and the reviews.

        Worth separating because the answer to it is different. A panel that
        is still building resolves on the next attempt; this does not — ten
        attempts on ten addresses returned it unchanged.
        """
        if not re.search(r"\d", rating_text):
            return False
        return not re.search(r"\(\s*[\d.,\s]+\s*\)", rating_text)

    def _place_panel_evidence(self, panel) -> List[str]:
        """
        Signs that this really is a rendered place panel, not a stalled page.

        Only actual place data counts — an address, a phone, a website, a plus
        code, a category. A tab labelled "About" is not evidence: the strip
        can be drawn while the content behind it never arrives, which is the
        exact case that must not be read as "this business has no reviews".

        Any one field is enough; the list is returned so the log records which
        proof was accepted.
        """
        found: List[str] = []
        for label, sel in (
            ("adres", 'button[data-item-id="address"]'),
            ("plus code", 'button[data-item-id="oloc"]'),
            ("kategori", 'button[jsaction*="category"]'),
            ("telefon", 'button[data-item-id^="phone"]'),
            ("site", 'a[data-item-id="authority"]'),
        ):
            try:
                if panel.find_elements(By.CSS_SELECTOR, sel):
                    found.append(label)
            except WebDriverException:
                continue

        # An address is the one field a place on a map effectively always
        # carries, so it stands alone. A phone or a website does not — plenty
        # of places have neither — so without an address two independent
        # fields are required before calling the panel rendered.
        if "adres" in found or len(found) >= 2:
            return found
        return []

    def _review_availability(self, driver: Chrome) -> str:
        """
        Decide between three answers, never two: "has", "none", "unknown".

        A place with no reviews really does lack a Reviews tab — Maps gives it
        Overview and About only — and calling that a failure throws away the
        info, photos and about sections that are right there. But absence of a
        tab is not proof of absence of reviews: a page that half-rendered, or
        one Google served stripped, looks exactly the same. Shrugging "no
        reviews then" at a broken page would quietly file empty packages as
        complete, which is worse than failing.

        So "none" is only returned on positive evidence — the tab strip IS
        there, and it has Overview/About without Reviews, and the header shows
        no rating. Anything unreadable is "unknown", and the caller treats
        that as a problem to capture and retry, not as a fact about the place.
        """
        # Waiting for "a tab strip exists" was waiting for the wrong thing.
        # Maps builds the panel in stages: Overview and About are in the strip
        # within a second or two, the Reviews tab arrives later. So the old
        # wait was satisfied by a strip that did not yet hold the answer, and
        # a place with 1099 reviews was read as a contradiction — rating
        # present, no Reviews tab — and failed. Captured DOM proved it: two
        # tabs, zero review cards, the rating block already there.
        #
        # Poll for a *settled* reading instead. Anything that is still
        # "unknown" is retried until the deadline, and only a page that never
        # settles is reported as unreadable.
        # "has" is positive evidence — a review card or a Reviews tab is
        # there, and nothing later takes it away — so it is accepted at once.
        # "none" is the absence of those, which is exactly what a panel that
        # is still building looks like, so it has to hold still: read it
        # twice, a gap apart, before believing it. That gap is the difference
        # between "this business has no reviews" and "the reviews had not
        # loaded yet", and only one of those is a fact.
        deadline = time.time() + TAB_STRIP_TIMEOUT
        none_since = None
        while True:
            answer = self._read_review_state(driver)
            if answer == "has":
                return "has"
            if answer == "withheld":
                return "withheld"
            if answer == "none":
                if none_since is None:
                    none_since = time.time()
                elif time.time() - none_since >= REVIEWLESS_CONFIRM:
                    return "none"
            else:
                none_since = None          # unsettled again; start over
            if time.time() >= deadline:
                return answer if answer == "none" and none_since else "unknown"
            time.sleep(1.0)

    def _read_review_state(self, driver: Chrome) -> str:
        """One reading of the panel; "unknown" means "not yet", not "broken"."""
        try:
            # Everything is read inside the place panel. The rest of the page
            # is a map full of OTHER businesses carrying their own star
            # ratings — a captured page for a place with no reviews at all
            # still held five of them — so an unscoped search finds a rating
            # for every place and answers "unclear" forever.
            panels = driver.find_elements(By.CSS_SELECTOR, 'div[role="main"]')
            if not panels:
                log.warning("No place panel (role=main) on the page")
                return "unknown"
            panel = panels[0]

            if panel.find_elements(By.CSS_SELECTOR, "div[data-review-id]"):
                return "has"
            tabs = panel.find_elements(By.CSS_SELECTOR, '[role="tab"]')
            labels = [((t.get_attribute("aria-label") or "") + " " + (t.text or "")).strip()
                      for t in tabs]
            if any(_text_contains_any(l.lower(), REVIEW_WORDS) for l in labels):
                return "has"
            # The place's OWN rating, read the way extract_header reads it:
            # the F7nice block inside the header container. Anything looser
            # picks up the neighbours — this very page carries four ratings
            # ("4.6 stars 798 Reviews" and friends) inside role=main, all of
            # them belonging to Maps' "people also search for" suggestions.
            rating_text = self._rating_block_text(driver)
            if self._reviews_withheld(rating_text):
                # Distinct from "still loading": the count is missing, which
                # is what the page looks like when the reviews module was not
                # sent at all. Retrying the same way does not change it.
                log.warning("Rating %r has no review count and there is no "
                            "reviews tab (%s) — reviews withheld",
                            rating_text, ", ".join(labels))
                return "withheld"
            if self._has_own_rating(driver):
                # A rating without a Reviews tab contradicts itself; do not
                # guess which half is true.
                log.warning("Rating present but no reviews tab (%s) — unclear",
                            ", ".join(labels))
                return "unknown"

            # Before calling it "no reviews", prove the panel actually
            # rendered this place: an About tab, or the fields every place
            # page carries. Otherwise a page that arrived half-built would be
            # filed as a business that simply has nothing to say.
            # A rendered panel with no reviews tab, no cards and no rating is
            # a business without reviews — including one with no tab strip at
            # all, which a captured page proved is a normal, fully rendered
            # state (photo, address, hours, site, phone, plus code, photos)
            # and not a sign of trouble. What must be proven is the panel, not
            # the tabs.
            evidence = self._place_panel_evidence(panel)
            if not evidence:
                log.warning("No place content could be read (tabs: %s) — "
                            "not treating this as a review-less place",
                            ", ".join(labels) or "none")
                return "unknown"
            log.info("Place panel rendered (%s), tabs %s, no rating — "
                     "this place has no reviews",
                     ", ".join(evidence), ", ".join(labels) or "none")
            return "none"
        except WebDriverException as e:
            log.warning(f"Could not read the tab strip: {e}")
            return "unknown"

    def _ensure_reviews_tab(self, driver: Chrome, attempts: int = 3,
                            per_attempt: float = 12.0) -> bool:
        """
        Click through to the reviews list and prove we got there.

        Returns False when the tab cannot be reached, so the caller can fail
        the place quickly and let it be retried elsewhere, instead of
        scrolling a pane that will never produce a card.
        """
        for attempt in range(1, attempts + 1):
            if self._on_reviews_tab(driver):
                if attempt > 1:
                    log.info("Reviews tab confirmed on attempt %d", attempt)
                return True
            log.info("Opening reviews tab (attempt %d/%d)", attempt, attempts)
            try:
                self.click_reviews_tab(driver)
            except Exception as e:  # noqa: BLE001
                log.warning(f"Reviews tab click raised: {e}")
            if self._wait_until(lambda: self._on_reviews_tab(driver), per_attempt):
                log.info("Reviews tab confirmed (attempt %d)", attempt)
                return True
            log.warning("Still not on the reviews list after attempt %d", attempt)
        return False

    @staticmethod
    def _current_ftid(driver: Chrome) -> Optional[str]:
        """
        The place's own hex id (`0x…:0x…`) as it appears in the canonical URL.

        This is the identity to compare against — not the requested Place ID.
        A place's ChIJ id lives in Maps' in-memory data, not in the DOM, so
        `driver.page_source` does not contain it and testing for it rejects
        perfectly good pages.
        """
        try:
            m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", driver.current_url)
        except Exception:  # noqa: BLE001
            return None
        return m.group(1) if m else None

    def _on_expected_place(self, driver: Chrome, expected_ftid: Optional[str],
                           context: str = "sayfa") -> bool:
        """
        Confirm a reload landed back on the same business.

        `expected_ftid` is captured on the first navigation, which went
        through `query_place_id` — Google resolved the Place ID itself there,
        so that page is authoritative by construction. Later reloads only have
        to match it.

        Returns True when the check passes or cannot be made (either id
        missing), False only when two known ids disagree.
        """
        if not expected_ftid:
            return True
        got = self._current_ftid(driver)
        if not got or got == expected_ftid:
            return True
        title = (driver.title or "").replace(" - Google Maps", "").strip()
        log.error("Wrong place loaded (%s): expected %s, got %s (%r) — "
                  "skipping to avoid mixing two businesses",
                  context, expected_ftid, got, title)
        return False

    def _extract_and_store_place_details(
        self, driver: Chrome, place_id: str,
        place_url: Optional[str] = None,
        review_count_fallback: Optional[int] = None,
        expected_ftid: Optional[str] = None,
    ):
        """
        Extract business details (general info, hours, price, popular times,
        about, owner photos) and persist them.

        Runs after the review scrape, so it reloads the place page first —
        always through `navigate_to_place`, never a bare `driver.get`, because
        a direct URL load serves logged-out visitors a stripped page where the
        photo carousel never renders.

        The owner-photo gallery runs dead last, after its own reload: it is the
        one step that can strand the browser (overlay + best-effort Back), and
        it needs the Overview carousel that the About tab click navigates away
        from. Failures are logged but never abort the run — the reviews are
        already stored by the time any of this happens.

        Every reload is identity-checked before anything is read: navigation
        does not always land on the same business (from a datacenter IP a
        place_id URL can resolve to an entirely different, local place), and
        writing another business's details over this one is far worse than
        having no details at all.
        """
        wait = WebDriverWait(driver, 20)
        want_photos = bool(self.config.get("scrape_place_photos", True))
        try:
            if place_url:
                log.info("Returning to place page for details...")
                self._report("details", step="mekan sayfasına dönülüyor")
                self.navigate_to_place(driver, place_url, wait)
                if not self._on_expected_place(driver, expected_ftid, "detay dönüşü"):
                    self._report("details", step="yanlış mekan — detaylar atlandı")
                    self.capture_failure(driver, "wrong_place_on_details",
                                         expected=expected_ftid)
                    self._flag("wrong_place_on_details",
                               "detay için dönülen sayfa başka bir mekan",
                               expected=expected_ftid,
                               got=self._current_ftid(driver))
                    return
                time.sleep(2)
                self.dismiss_cookies(driver)

            self._report("details", step="bilgi · saat · fiyat · yoğunluk · about")
            log.info("Extracting place details (info, hours, price, about)...")
            details = extract_place_details(
                driver,
                include_photos=False,        # gallery handled below, last
                photos_limit=self.config.get("place_photos_limit", 60),
            )
            details["place_id"] = place_id
            if details.get("review_count") is None and review_count_fallback:
                details["review_count"] = review_count_fallback
                log.info("Review count taken from reviews tab: %d",
                         review_count_fallback)

            if want_photos:
                self._report("photos", step="owner galerisi")
                if place_url:
                    # back to Overview — the About click above left it
                    self.navigate_to_place(driver, place_url, wait)
                    time.sleep(2)
                try:
                    details["owner_photos"] = extract_owner_photos(
                        driver,
                        limit=self.config.get("place_photos_limit", 60),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Owner photo extraction failed (continuing): {e}")
                    details["owner_photos"] = None

            # Download owner photos through the hardened image session
            # (browser cookies forwarded — required for newer CDN tokens).
            photos = (details.get("owner_photos") or {}).get("photos") or []
            if photos and self.config.get("download_place_photos", True):
                try:
                    from workers.engine.image_handler import ImageHandler
                    handler = ImageHandler(self.config)
                    try:
                        handler.apply_browser_cookies(driver.get_cookies())
                    except Exception:
                        pass
                    self._report("photos", step="owner fotoğrafları indiriliyor",
                                 total=len(photos))
                    handler.download_place_photos(place_id, photos)
                except Exception as e:
                    log.warning(f"Owner photo download failed (continuing): {e}")

            self.review_db.upsert_place_details(place_id, details)

            summary = []
            if details.get("rating") is not None:
                summary.append(f"rating={details['rating']}")
            if details.get("review_count") is not None:
                summary.append(f"reviews={details['review_count']}")
            if details.get("price", {}) and details["price"].get("available"):
                summary.append("price=yes")
            hours = details.get("opening_hours") or {}
            summary.append(f"hours={'yes' if hours.get('available') else 'no'}")
            about = details.get("about") or {}
            summary.append(f"about_sections={len(about.get('sections') or [])}")
            op = details.get("owner_photos") or {}
            summary.append(f"owner_photos={op.get('count', 0)}")
            log.info("Place details stored (%s)", ", ".join(summary))

            self._write_place_details_json(place_id, details)
        except Exception as e:
            log.warning(f"Place details extraction failed (continuing): {e}")

    def _write_place_details_json(self, place_id: str, details: Dict[str, Any]):
        """Merge this place's details into the place-details JSON file."""
        path = self.config.get("place_details_json_path", "place_details.json")
        if not path:
            return
        try:
            existing: Dict[str, Any] = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            existing[place_id] = details
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            log.info(f"Place details JSON written to {path}")
        except Exception as e:
            log.warning(f"Could not write place details JSON: {e}")

    def _navigate_by_place_id(self, driver: Chrome, url: str) -> bool:
        """
        Open a place through Google's Maps URLs endpoint:

            /maps/search/?api=1&query=<id>&query_place_id=<id>&hl=..&gl=..

        `query_place_id` pins the result to that exact place regardless of
        where the request comes from, and Maps redirects to the canonical
        /maps/place/<name>/@lat,lng/data=!..!1s0x<ftid> URL — which is what
        lets extract_place_id() return a real id instead of a hash.

        `gl` (config: region) biases results to a country; it is not needed
        for correctness here but keeps formatting stable across VM regions.

        Returns True only if the page really is the requested place.
        """
        place_id = self._requested_place_id(url)
        if not place_id:
            return False
        target = (f"https://www.google.com/maps/search/?api=1&query={place_id}"
                  f"&query_place_id={place_id}")
        region = (self.config.get("region") or "").strip()
        if region:
            target += f"&gl={region}"
        target = self._with_hl(target)
        try:
            log.info(f"Opening place by id: {target}")
            driver.get(target)
            # Clear any consent wall FIRST. It blocks the redirect, so waiting
            # for the canonical URL before dismissing it means waiting out the
            # whole timeout for something that cannot happen — and then
            # falling back to the name search, which is the path that fails to
            # open the reviews tab.
            time.sleep(1)
            self.dismiss_cookies(driver)
            # Then wait for the redirect to land, rather than sleeping a fixed
            # guess: on a slow VM four seconds was often not enough.
            reached = self._wait_until(
                lambda: "/maps/place/" in driver.current_url, PLACE_URL_TIMEOUT)
            if not reached:
                # a consent dialog can also appear late, after the first load
                self.dismiss_cookies(driver)
                reached = self._wait_until(
                    lambda: "/maps/place/" in driver.current_url, 8)
            # Reaching a canonical /maps/place/ URL from query_place_id IS the
            # proof of identity: Google did the Place ID -> place resolution.
            if not reached:
                log.warning("Place id navigation did not reach a canonical "
                            "place URL in %ss — falling back to search",
                            PLACE_URL_TIMEOUT)
                return False
            # the map still needs a moment to render its panels
            time.sleep(2)
            log.info(f"Place opened by id: {(driver.title or '').strip()} "
                     f"[{self._current_ftid(driver)}]")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning(f"Place id navigation failed ({e}) — falling back")
            return False

    def navigate_to_place(self, driver: Chrome, url: str, wait: WebDriverWait) -> bool:
        """
        Navigate to a Google Maps place, bypassing the 'limited view' restriction
        that Google shows to non-logged-in users.

        Strategy:
        0. When the request carries a Place ID, use Google's documented Maps
           URLs endpoint (`?api=1&query_place_id=`), which pins the result to
           that exact place and redirects to the canonical /maps/place/ URL.
        1. Warm up by visiting google.com to establish cookies/session state
        2. Use Google Maps search-based navigation (avoids limited view)
        3. Fall back to direct URL if search doesn't work

        Step 0 exists because name-based search is not identity-preserving:
        from a datacenter IP it can return a different, local business, and it
        leaves the URL unresolved so the place id degrades to a hash.
        """
        log.info("Navigating to place with limited-view bypass...")

        # Step 0: Maps URLs API — exact, documented, and redirects to canonical
        if self._navigate_by_place_id(driver, url):
            return True

        # Step 1: Warm up - visit google.com first to establish session cookies
        try:
            driver.get(self._with_hl("https://www.google.com"))
            time.sleep(2)
            self.dismiss_cookies(driver)
            log.info("Session warm-up completed")
        except Exception as e:
            log.debug(f"Warm-up navigation failed: {e}")

        # Step 2: Resolve the target URL and extract place name
        place_name = self._extract_place_name(driver, url)
        current_url = driver.current_url

        # Step 3: Try search-based navigation (primary bypass method)
        if place_name:
            # Extract coordinates for more precise search
            lat, lng = self._extract_place_coords(current_url)
            search_query = place_name
            if lat and lng:
                search_url = f"https://www.google.com/maps/search/{search_query}/@{lat},{lng},17z"
            else:
                search_url = f"https://www.google.com/maps/search/{search_query}/"
            search_url = self._with_hl(search_url)

            log.info(f"Trying search-based navigation: {search_url}")
            driver.get(search_url)
            time.sleep(5)

            # Check if we landed on a place page with full content (tabs visible)
            tabs = driver.find_elements(By.CSS_SELECTOR, '[role="tab"]')
            has_reviews = any(
                any(w in (t.text or "").lower() for w in REVIEW_WORDS)
                or t.get_attribute("data-tab-index") == "1"
                for t in tabs
            )

            if has_reviews:
                log.info("Search-based navigation successful - full page with reviews tab loaded")
                self.dismiss_cookies(driver)
                return True

            # Check for review cards directly (some layouts skip tabs)
            cards = driver.find_elements(By.CSS_SELECTOR, 'div[data-review-id]')
            if cards:
                log.info(f"Search-based navigation found {len(cards)} review cards")
                self.dismiss_cookies(driver)
                return True

            log.info("Search-based navigation did not show reviews, trying direct URL...")

        # Step 4: Fallback to direct URL
        url = self._with_hl(url)
        log.info(f"Navigating directly to: {url}")
        driver.get(url)
        try:
            wait.until(lambda d: "google.com/maps" in d.current_url)
        except TimeoutException:
            log.warning("Timed out waiting for Google Maps to load")
        time.sleep(3)
        self.dismiss_cookies(driver)

        # Check if limited view is active. Multilingual check + structural
        # signal (presence of a Sign-in prompt) makes this robust for the
        # French/German/non-English cases reported in issue #15.
        if self._is_limited_view(driver):
            log.warning(
                "Google Maps is showing a limited view — reviews may be unavailable"
            )

        return True

    # Localized "limited view" strings. Not exhaustive — the structural
    # sign-in detection in _is_limited_view() is the primary signal.
    _LIMITED_VIEW_STRINGS = (
        "limited view",
        "vue limitée",                  # French
        "eingeschränkte ansicht",       # German
        "vista limitada",               # Spanish / Portuguese
        "vista limitata",               # Italian
        "תצוגה מוגבלת",                 # Hebrew
        "มุมมองที่จำกัด",                # Thai
        "ограниченный просмотр",        # Russian
        "限定ビュー",                    # Japanese
        "제한된 보기",                   # Korean
        "受限视图", "受限檢視",           # Chinese
        "عرض محدود",                     # Arabic
        "sınırlı görünüm",              # Turkish
        "ograniczony widok",            # Polish
        "beperkte weergave",            # Dutch
    )

    def _is_limited_view(self, driver: Chrome) -> bool:
        """Detect limited-view restriction across languages + structure."""
        try:
            body_text = (
                driver.find_element(By.TAG_NAME, "body").text or ""
            ).lower()
        except Exception:  # noqa: BLE001
            return False

        for phrase in self._LIMITED_VIEW_STRINGS:
            if phrase in body_text:
                return True

        # Structural: the sign-in prompt is shown on limited-view pages.
        # If it's visible AND review tab selectors are absent, we treat it
        # as limited-view regardless of the exact locale.
        try:
            sign_in_visible = bool(driver.find_elements(
                By.CSS_SELECTOR,
                'a[data-action="sign in"], a[href*="ServiceLogin"]',
            ))
            tab_present = bool(driver.find_elements(
                By.CSS_SELECTOR, '[role="tab"]'
            ))
            if sign_in_visible and not tab_present:
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # Minimum score needed to accept a tab as the Reviews tab.
    # Tuned so that aria-label match (1.5) alone clears the bar, but
    # data-tab-index="1" alone (no keyword match, or with a menu-like label)
    # does not. Configurable via config.yaml adaptive.tab_detection_threshold.
    TAB_DETECTION_THRESHOLD = 1.5

    def is_reviews_tab(self, tab: WebElement) -> bool:
        """
        Score `tab` against multiple signals and accept only if the total
        score ≥ threshold.

        Fixes the long-standing bug where `data-tab-index="1"` alone caused
        the Menu tab to be accepted on places that have both Menu and Reviews
        (issues #21, #17, #15).
        """
        try:
            score = self._score_reviews_tab(tab)
            threshold = self.config.get("adaptive", {}).get(
                "tab_detection_threshold", self.TAB_DETECTION_THRESHOLD
            )
            return score >= threshold
        except StaleElementReferenceException:
            return False
        except Exception as e:
            log.debug(f"is_reviews_tab error: {e}")
            return False

    def _score_reviews_tab(self, tab: WebElement) -> float:
        """Weighted scoring for tab-is-reviews detection."""
        aria_label = (tab.get_attribute("aria-label") or "").lower()
        tab_text = (tab.text or "").lower()
        tab_index = tab.get_attribute("data-tab-index") or ""

        score = 0.0

        # Strongest signals — explicit semantic match.
        if _text_contains_any(aria_label, REVIEW_WORDS):
            score += 1.5
        if _text_contains_any(tab_text, REVIEW_WORDS):
            score += 1.0

        # Penalize non-review labels — prevents Menu/Overview misclassification
        # when they happen to sit at data-tab-index="1".
        if _text_contains_any(aria_label, NON_REVIEW_TAB_WORDS):
            score -= 1.5
        if _text_contains_any(tab_text, NON_REVIEW_TAB_WORDS):
            score -= 1.0

        # Weak positive: index + keyword already scored above; bare index
        # without any keyword is no longer sufficient.
        if tab_index in ("1", "reviews") and score > 0:
            score += 0.25

        # URL-ish attributes — strong signal, matches aria-label weight.
        for attr in ("href", "data-href", "data-url", "data-target"):
            val = (tab.get_attribute(attr) or "").lower()
            if val and ("review" in val or "rating" in val):
                score += 1.5
                break

        # Class-name hint (weakest — Google reuses class names across tabs).
        tab_class = (tab.get_attribute("class") or "").lower()
        if any(c in tab_class for c in ("review", "rating", "g4jrve")):
            score += 0.5

        return score

    def click_reviews_tab(self, driver: Chrome):
        """
        Highly dynamic reviews tab detection and clicking with multiple fallback strategies.
        Works across different languages, layouts, and browser environments.
        """
        max_timeout = 25  # Maximum seconds to try
        end_time = time.time() + max_timeout
        attempts = 0

        # Selector order matters — highest-specificity first.
        # NOTE: `[data-tab-index="1"]` is deliberately NOT first (see #21).
        # Scoring in is_reviews_tab() would still reject Menu, but putting
        # semantically targeted selectors first avoids scanning the wrong
        # element set at all.
        tab_selectors = [
            # Strongest: explicit aria-label match, any language.
            '[role="tab"][aria-label*="review" i]',
            '[role="tab"][aria-label*="avis" i]',
            '[role="tab"][aria-label*="bewertung" i]',
            '[role="tab"][aria-label*="reseña" i]',
            '[role="tab"][aria-label*="recensione" i]',
            '[role="tab"][aria-label*="ביקורת"]',
            '[role="tab"][aria-label*="リビュー"]',
            '[role="tab"][aria-label*="рецензии"]',

            # Any tab in the tablist — scoring filters them.
            '[role="tab"][data-tab-index]',
            'button[role="tab"]',
            'div[role="tab"]',
            'a[role="tab"]',

            # Google Maps-specific class patterns (legacy fallback).
            '.fontTitleSmall[role="tab"]',
            '.hh2c6[role="tab"]',
            '.m6QErb [role="tab"]',
            'div[role="tablist"] > *',
            'div.m6QErb div[role="tablist"] > *',

            # Absolute last resort — index-based. Scoring still applies.
            '[data-tab-index="1"]',
        ]

        # Record successful clicks for debugging
        successful_method = None
        successful_selector = None

        # Try each selector in turn
        for selector in tab_selectors:
            if time.time() > end_time:
                break

            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if not elements:
                    self._record_selector(selector, "miss")
                    continue
                self._record_selector(selector, "hit")

                # Try each element found with this selector
                for element in elements:
                    attempts += 1

                    # First check if this is actually a reviews tab
                    if not self.is_reviews_tab(element):
                        continue

                    # Found a reviews tab, attempt to click it with multiple methods
                    log.info(f"Found potential reviews tab ({selector}): '{element.text}', attempting to click")

                    # Ensure visibility
                    driver.execute_script("arguments[0].scrollIntoView({block:'center', behavior:'smooth'});", element)
                    time.sleep(0.7)  # Wait for scroll

                    # Try different click methods in order of reliability
                    click_methods = [
                        # Method 1: JavaScript click (most reliable)
                        lambda: driver.execute_script("arguments[0].click();", element),

                        # Method 2: Direct click
                        lambda: element.click(),

                        # Method 3: ActionChains click
                        lambda: ActionChains(driver).move_to_element(element).click().perform(),

                        # Method 4: Send RETURN key
                        lambda: element.send_keys(Keys.RETURN),

                        # Method 5: Center click with ActionChains
                        lambda: ActionChains(driver).move_to_element_with_offset(
                            element, element.size['width'] // 2, element.size['height'] // 2).click().perform(),
                    ]

                    # Try each click method
                    for i, click_method in enumerate(click_methods):
                        try:
                            click_method()
                            time.sleep(1.5)  # Wait for click to take effect

                            # Verify if click worked (check for new content)
                            if self.verify_reviews_tab_clicked(driver):
                                successful_method = i + 1
                                successful_selector = selector
                                log.info(
                                    f"Successfully clicked reviews tab using method {i + 1} and selector '{selector}'")
                                return True
                        except Exception as click_error:
                            log.debug(f"Click method {i + 1} failed: {click_error}")
                            continue

            except Exception as selector_error:
                log.debug(f"Error with selector '{selector}': {selector_error}")
                continue

        # If we reach here, try XPath as a last resort
        if time.time() <= end_time:
            for language_keyword in REVIEW_WORDS:
                try:
                    # Try XPath contains text
                    xpath = f"//*[contains(text(), '{language_keyword}')]"
                    elements = driver.find_elements(By.XPATH, xpath)

                    for element in elements:
                        try:
                            log.info(f"Trying XPath with keyword '{language_keyword}'")
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                            time.sleep(0.7)
                            driver.execute_script("arguments[0].click();", element)
                            time.sleep(1.5)

                            if self.verify_reviews_tab_clicked(driver):
                                log.info(f"Successfully clicked element with keyword '{language_keyword}'")
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue

        # Final attempt: try to navigate directly to reviews by URL
        try:
            current_url = driver.current_url
            if "?hl=" in current_url:  # Preserve language setting if present
                lang_param = re.search(r'\?hl=([^&]*)', current_url)
                if lang_param:
                    lang_code = lang_param.group(1)
                    # Try to replace the current part with 'reviews' or append it
                    if '/place/' in current_url:
                        parts = current_url.split('/place/')
                        new_url = f"{parts[0]}/place/{parts[1].split('/')[0]}/reviews?hl={lang_code}"
                        driver.get(new_url)
                        time.sleep(3)  # Increased wait time for page load
                        if "review" in driver.current_url.lower():
                            log.info("Navigated directly to reviews page via URL")
                            # Extra wait for reviews to render after URL navigation
                            time.sleep(2)
                            return True

            # Try to identify reviews link in URL
            if '/place/' in current_url and '/reviews' not in current_url:
                parts = current_url.split('/place/')
                new_url = f"{parts[0]}/place/{parts[1].split('/')[0]}/reviews"
                driver.get(new_url)
                time.sleep(3)  # Increased wait time for page load
                if "review" in driver.current_url.lower():
                    log.info("Navigated directly to reviews page via URL")
                    # Extra wait for reviews to render after URL navigation
                    time.sleep(2)
                    return True
        except Exception as url_error:
            log.warning(f"Failed to navigate to reviews via URL: {url_error}")

        log.warning(f"Failed to find/click reviews tab after {attempts} attempts")
        raise TimeoutException("Reviews tab not found or could not be clicked")

    def verify_reviews_tab_clicked(self, driver: Chrome) -> bool:
        """
        Did the click actually open the reviews list?

        This used to accept the generic pane class shared with Overview, and
        any star rating anywhere on the page — which on Maps means the
        neighbours listed under "people also search for". Both answered yes on
        a page showing no reviews at all, which is how a scrape came to scroll
        an empty Overview pane to the end of its patience. It now asks the
        same question as the rest of the flow, and only review-specific
        evidence counts.
        """
        return self._on_reviews_tab(driver)

    def set_sort(self, driver: Chrome, method: str):
        """
        Set the sorting method for reviews with enhanced detection for the latest Google Maps UI.
        Works across different languages and UI variations, with robust error handling.
        """
        if method == "relevance":
            log.info("Using default 'relevance' sort - no need to change sort order")
            return True  # Default order, no need to change

        log.info(f"Attempting to set sort order to '{method}'")

        try:
            # 1. Find and click the sort button
            sort_button_selectors = [
                # Exact selectors based on recent HTML structure
                'button.HQzyZ[aria-haspopup="true"]',
                'div.m6QErb button.HQzyZ',
                'button[jsaction*="pane.wfvdle84"]',
                'div.fontBodyLarge.k5lwKb',  # The text element inside sort button

                # Common attribute-based selectors
                'button[aria-label*="Sort" i]',
                'button[aria-label*="sort" i]',
                'button[aria-expanded="false"][aria-haspopup="true"]',

                # Multilingual selectors
                'button[aria-label*="סדר" i]',  # Hebrew
                'button[aria-label*="เรียง" i]',  # Thai
                'button[aria-label*="排序" i]',  # Chinese
                'button[aria-label*="Trier" i]',  # French
                'button[aria-label*="Ordenar" i]',  # Spanish/Portuguese
                'button[aria-label*="Sortieren" i]',  # German

                # Parent container-based selectors
                'div.m6QErb.Hk4XGb.XiKgde.tLjsW button',
                'div.m6QErb div.XiKgde button'
            ]

            # Attempt to find the sort button
            # Every candidate is collected before one is chosen. The search
            # used to take the first element any selector returned, and on a
            # venue inside a hotel that was one of five wrong answers — all of
            # them the hotel's own controls, matched because "sort" hides
            # inside "Resort". A sort control is a single thing on the page;
            # more than one candidate means the rule is matching something
            # else, and that is worth reporting rather than guessing through.
            candidates = []
            seen_ids = set()
            for selector in sort_button_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:  # noqa: BLE001
                    continue
                for element in elements:
                    try:
                        if not element.is_displayed() or not element.is_enabled():
                            continue
                        eid = element.id
                        if eid in seen_ids:
                            continue

                        button_text = element.text.strip() if element.text else ""
                        button_aria = element.get_attribute("aria-label") or ""
                        button_class = element.get_attribute("class") or ""

                        # Negative words are matched as words too: a place
                        # called "Backyard by Olde" puts "back" inside the
                        # label of its own perfectly good sort control.
                        if _has_any_word(button_text, NON_SORT_WORDS) or \
                                _has_any_word(button_aria, NON_SORT_WORDS):
                            continue

                        has_sort_keyword = _has_sort_word(button_text) or \
                            _has_sort_word(button_aria)
                        has_sort_class = ("HQzyZ" in button_class
                                          or "sort" in button_class.lower())
                        if not (has_sort_keyword or has_sort_class):
                            continue

                        has_dropdown_attrs = (
                            element.get_attribute("aria-haspopup") == "true"
                            or element.get_attribute("aria-expanded") is not None)
                        seen_ids.add(eid)
                        candidates.append({
                            "el": element, "selector": selector,
                            "text": button_text, "aria": button_aria,
                            "class": button_class,
                            # A dropdown attribute is not evidence on its own —
                            # plenty of buttons carry aria-expanded — but it
                            # separates a real menu from a link when the name
                            # already matched.
                            "score": (2 if has_sort_class else 0)
                                     + (1 if has_dropdown_attrs else 0),
                        })
                    except Exception as e:  # noqa: BLE001
                        log.debug(f"Error checking element: {e}")

            sort_button = None
            if len(candidates) == 1:
                sort_button = candidates[0]["el"]
                log.info("Found sort button: %r (aria=%r) via %s",
                         candidates[0]["text"][:60], candidates[0]["aria"][:60],
                         candidates[0]["selector"])
            elif len(candidates) > 1:
                candidates.sort(key=lambda c: c["score"], reverse=True)
                etiketler = [f"{c['aria'] or c['text']}"[:70] for c in candidates]
                log.warning("Sort control is ambiguous — %d candidates: %s",
                            len(candidates), " | ".join(etiketler))
                self.capture_failure(driver, "sort_button_ambiguous",
                                     candidates=etiketler)
                self._flag("sort_button_ambiguous",
                           f"{len(candidates)} sıralama düğmesi adayı bulundu; "
                           "kural yanlış öğeyi de yakalıyor olabilir",
                           candidates=etiketler)
                sort_button = candidates[0]["el"]

            # If no button found with CSS selectors, try finding it from its container
            if not sort_button:
                try:
                    # Look for the sort container by its distinctive classes
                    containers = driver.find_elements(By.CSS_SELECTOR, 'div.m6QErb.Hk4XGb, div.XiKgde.tLjsW')
                    for container in containers:
                        try:
                            # Find buttons within this container
                            # Taking the first visible button in a container
                            # is a guess about layout, not a identification —
                            # it is how "More info" got clicked six ways. The
                            # button still has to say it sorts.
                            buttons = container.find_elements(By.TAG_NAME, 'button')
                            for button in buttons:
                                if not (button.is_displayed() and button.is_enabled()):
                                    continue
                                label = ((button.get_attribute("aria-label") or "")
                                         + " " + (button.text or ""))
                                if any(k in label.lower() for k in SORT_WORDS):
                                    sort_button = button
                                    log.info(f"Found sort button in container: '{label.strip()}'")
                                    break
                        except Exception:
                            continue
                        if sort_button:
                            break
                except Exception as e:
                    log.debug(f"Error finding button via container: {e}")

            # If still no button found, try XPath approach with keywords
            if not sort_button:
                xpath_terms = ["sort", "Sort", "סדר", "סידור", "เรียง", "排序", "Trier", "Ordenar", "Sortieren"]
                for term in xpath_terms:
                    try:
                        xpath = f"//*[contains(text(), '{term}') or contains(@aria-label, '{term}')]"
                        elements = driver.find_elements(By.XPATH, xpath)
                        for element in elements:
                            try:
                                if element.is_displayed() and element.is_enabled():
                                    sort_button = element
                                    log.info(f"Found sort button with XPath term: '{term}'")
                                    break
                            except Exception:
                                continue
                        if sort_button:
                            break
                    except Exception:
                        continue
            
            # Final fallback: look for any button in the reviews area that might open a dropdown
            if not sort_button:
                try:
                    # Look specifically in the reviews container area
                    reviews_container = driver.find_elements(By.CSS_SELECTOR, 'div.m6QErb, div.DxyBCb')
                    for container in reviews_container:
                        try:
                            # Find all buttons in this container
                            buttons = container.find_elements(By.TAG_NAME, 'button')
                            for button in buttons:
                                try:
                                    if (button.is_displayed() and button.is_enabled() and
                                        (button.get_attribute("aria-haspopup") == "true" or
                                         "dropdown" in (button.get_attribute("class") or "").lower())):
                                        sort_button = button
                                        log.info("Found potential sort button via fallback dropdown detection")
                                        break
                                except Exception:
                                    continue
                            if sort_button:
                                break
                        except Exception:
                            continue
                except Exception as e:
                    log.debug(f"Error in fallback sort button detection: {e}")

            # Final check - do we have a sort button?
            if not sort_button:
                log.warning("No sort button found with any method - keeping default sort order")
                return False

            # 2. Click the sort button to open dropdown menu

            # First ensure the button is in view
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", sort_button)
            time.sleep(0.8)  # Wait for scroll

            # Try multiple click methods
            click_methods = [
                # Method 1: JavaScript click
                lambda: driver.execute_script("arguments[0].click();", sort_button),

                # Method 2: Direct click
                lambda: sort_button.click(),

                # Method 3: ActionChains click with move first
                lambda: ActionChains(driver).move_to_element(sort_button).pause(0.3).click().perform(),

                # Method 4: Click on center of element
                lambda: ActionChains(driver).move_to_element_with_offset(
                    sort_button, sort_button.size['width'] // 2, sort_button.size['height'] // 2
                ).click().perform(),

                # Method 5: JavaScript focus and click
                lambda: driver.execute_script(
                    "arguments[0].focus(); setTimeout(function() { arguments[0].click(); }, 100);", sort_button
                ),

                # Method 6: Send RETURN key after focusing
                lambda: ActionChains(driver).move_to_element(sort_button).click().send_keys(Keys.RETURN).perform()
            ]

            # Try each click method
            menu_opened = False

            for i, click_method in enumerate(click_methods):
                try:
                    log.info(f"Trying click method {i + 1} for sort button...")
                    click_method()
                    time.sleep(1)  # Wait for menu to appear

                    # Check if menu opened
                    menu_opened = self.check_if_menu_opened(driver)

                    if menu_opened:
                        log.info(f"Sort menu opened with click method {i + 1}")
                        break
                except Exception as e:
                    log.debug(f"Click method {i + 1} failed: {e}")
                    continue

            # If menu not opened, abort
            if not menu_opened:
                log.warning("Failed to open sort menu - keeping default sort order")
                # Try to reset state by clicking elsewhere
                try:
                    ActionChains(driver).move_by_offset(50, 50).click().perform()
                except Exception:
                    pass
                return False

            # 3. Find and click the desired sort option in the menu

            # Selectors for menu items with focus on the exact HTML structure
            menu_item_selectors = [
                # Exact Google Maps menu item selectors
                'div[role="menuitemradio"]',
                'div.fxNQSd[role="menuitemradio"]',
                'div[role="menuitemradio"] div.mLuXec',  # Inner text container

                # Generic menu item selectors (fallback)
                '[role="menuitemradio"]',
                '[role="menuitem"]',
                'div[role="menu"] > div'
            ]

            # Combined selector for efficiency
            combined_selector = ", ".join(menu_item_selectors)

            try:
                # Wait for menu items to appear
                menu_items = WebDriverWait(driver, 5).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, combined_selector))
                )

                # Process menu items to find matches
                visible_items = []

                for item in menu_items:
                    try:
                        # Skip invisible items
                        if not item.is_displayed():
                            continue

                        # Handle different element types
                        if item.get_attribute('role') == 'menuitemradio':
                            # This is a top-level menu item
                            try:
                                # Try to find text in the inner div.mLuXec element first
                                text_elements = item.find_elements(By.CSS_SELECTOR, 'div.mLuXec')
                                if text_elements and text_elements[0].is_displayed():
                                    text = text_elements[0].text.strip()
                                    visible_items.append((item, text))
                                else:
                                    # Fall back to the item's own text
                                    text = item.text.strip()
                                    visible_items.append((item, text))
                            except Exception:
                                # Last resort - use the item's own text
                                text = item.text.strip()
                                visible_items.append((item, text))
                        elif 'mLuXec' in (item.get_attribute('class') or ''):
                            # This is the text container element - get its parent menuitemradio
                            try:
                                text = item.text.strip()
                                parent = driver.execute_script(
                                    "return arguments[0].closest('[role=\"menuitemradio\"]');",
                                    item
                                )
                                if parent:
                                    visible_items.append((parent, text))
                            except Exception:
                                continue
                        else:
                            # Generic menu item handling
                            text = item.text.strip()
                            visible_items.append((item, text))
                    except Exception as e:
                        log.debug(f"Error processing menu item: {e}")
                        continue

                # Deduplicate: keep one entry per underlying DOM element,
                # skip container elements whose text spans multiple labels
                seen_elems = set()
                deduped = []
                for elem, text in visible_items:
                    eid = elem.id  # Selenium's internal element id (stable per session)
                    if eid in seen_elems or not text or "\n" in text:
                        continue
                    seen_elems.add(eid)
                    deduped.append((elem, text))
                visible_items = deduped

                log.info(f"Found {len(visible_items)} menu items: {[t for _, t in visible_items]}")

                # --- Strategy A: text-first matching (robust against reordering) ---
                target_item = None
                matched_text = None
                wanted_labels = [lbl.lower() for lbl in SORT_OPTIONS.get(method, [])]

                for item, text in visible_items:
                    if text.lower() in wanted_labels:
                        target_item = item
                        matched_text = text
                        log.info(f"Matched sort '{method}' by text: '{text}'")
                        break

                # --- Strategy B: position fallback (only if text match failed) ---
                if not target_item:
                    position_map = {
                        "relevance": 0,
                        "newest": 1,
                        "highest": 2,
                        "lowest": 3,
                    }
                    pos = position_map.get(method, -1)
                    if 0 <= pos < len(visible_items):
                        target_item, matched_text = visible_items[pos]
                        log.info(f"Position fallback {pos + 1}: '{matched_text}' for '{method}'")
                    else:
                        log.warning(f"Could not find sort '{method}' by text or position")

                # 3. If target found, click it
                if target_item:
                    # Ensure item is in view
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_item)
                    time.sleep(0.3)

                    # Try multiple click methods
                    click_success = False
                    click_methods = [
                        # Method 1: JavaScript click
                        lambda: driver.execute_script("arguments[0].click();", target_item),

                        # Method 2: Direct click
                        lambda: target_item.click(),

                        # Method 3: ActionChains click
                        lambda: ActionChains(driver).move_to_element(target_item).click().perform(),

                        # Method 4: Center click
                        lambda: ActionChains(driver).move_to_element_with_offset(
                            target_item, target_item.size['width'] // 2, target_item.size['height'] // 2
                        ).click().perform(),

                        # Method 5: JavaScript click with custom event
                        lambda: driver.execute_script("""
                            var el = arguments[0];
                            var evt = new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                view: window
                            });
                            el.dispatchEvent(evt);
                        """, target_item)
                    ]

                    for i, click_method in enumerate(click_methods):
                        try:
                            click_method()
                            time.sleep(1.5)  # Wait for sort to take effect

                            # Try to verify sort happened by checking if menu closed
                            still_open = self.check_if_menu_opened(driver)
                            if not still_open:
                                click_success = True
                                log.info(f"Successfully clicked menu item with method {i + 1}")
                                break
                        except Exception as e:
                            log.debug(f"Menu item click method {i + 1} failed: {e}")
                            continue

                    if click_success:
                        # Validate: does the matched text belong to our wanted labels?
                        if matched_text and matched_text.lower() in wanted_labels:
                            log.info(f"Sort confirmed: '{method}'")
                            return True
                        log.warning(
                            f"Sort clicked '{matched_text}' but could not confirm it matches '{method}'"
                        )
                        return False
                    else:
                        log.warning(f"Failed to click menu item - keeping default sort order")
                else:
                    log.warning(f"No matching menu item found for '{method}'")

                # If we get here, we failed - try to close the menu by clicking elsewhere
                try:
                    ActionChains(driver).move_by_offset(50, 50).click().perform()
                except Exception:
                    pass

                return False

            except TimeoutException:
                log.warning("Timeout waiting for menu items")
                return False
            except Exception as e:
                log.warning(f"Error in menu item selection: {e}")
                return False

        except Exception as e:
            log.warning(f"Error in set_sort method: {e}")
            return False

    def check_if_menu_opened(self, driver):
        """
        Check if a sort menu has been opened after clicking the sort button.
        Uses multiple detection strategies optimized for Google Maps dropdowns.
        Returns True if menu is detected, False otherwise.
        """
        try:
            # 1. First check for exact menu container selectors from the latest Google Maps UI
            specific_menu_selectors = [
                'div[role="menu"][id="action-menu"]',  # Exact match from provided HTML
                'div.fontBodyLarge.yu5kgd[role="menu"]',  # Classes from provided HTML
                'div.fxNQSd[role="menuitemradio"]',  # Menu item class
                'div.yu5kgd[role="menu"]'  # Alternate class
            ]

            for selector in specific_menu_selectors:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        if element.is_displayed():
                            return True
                    except Exception:
                        continue

            # 2. Check for generic menu containers
            generic_menu_selectors = [
                'div[role="menu"]',
                'ul[role="menu"]',
                '[role="listbox"]'
            ]

            for selector in generic_menu_selectors:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        if element.is_displayed():
                            return True
                    except Exception:
                        continue

            # 3. Look for menu items
            menu_item_selectors = [
                'div[role="menuitemradio"]',  # Google Maps specific
                'div.fxNQSd',  # Class-based detection
                'div.mLuXec',  # Text container class
                '[role="menuitem"]',  # Generic menu items
                '[role="option"]'  # Alternative role
            ]

            visible_items = 0
            for selector in menu_item_selectors:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        if element.is_displayed():
                            visible_items += 1
                            if visible_items >= 2:  # At least 2 menu items should be visible
                                return True
                    except Exception:
                        continue

            # 4. Advanced detection with JavaScript
            # Checks if there are newly visible elements with menu-related roles or classes
            try:
                js_detection = """
                return (function() {
                    // Check for visible menu elements
                    var menuElements = document.querySelectorAll('div[role="menu"], div[role="menuitemradio"], div.fxNQSd');
                    for (var i = 0; i < menuElements.length; i++) {
                        var style = window.getComputedStyle(menuElements[i]);
                        if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                            return true;
                        }
                    }

                    // Check for any recently appeared elements that might be a menu
                    var possibleMenus = document.querySelectorAll('div.yu5kgd, div.fontBodyLarge');
                    for (var i = 0; i < possibleMenus.length; i++) {
                        var style = window.getComputedStyle(possibleMenus[i]);
                        var rect = possibleMenus[i].getBoundingClientRect();
                        // Check if element is visible and has a meaningful size
                        if (style.display !== 'none' && style.visibility !== 'hidden' && 
                            rect.width > 50 && rect.height > 50) {
                            return true;
                        }
                    }

                    return false;
                })();
                """
                menu_detected = driver.execute_script(js_detection)
                if menu_detected:
                    return True
            except Exception as js_error:
                log.debug(f"Error in JavaScript menu detection: {js_error}")

            # 5. Last resort: check if any positioning styles were applied to elements
            # This can detect menu containers that have been positioned absolutely
            try:
                position_check = """
                return (function() {
                    // Look for absolutely positioned elements that appeared recently
                    var elements = document.querySelectorAll('div[style*="position: absolute"]');
                    for (var i = 0; i < elements.length; i++) {
                        var el = elements[i];
                        var style = window.getComputedStyle(el);
                        var hasMenuItems = el.querySelectorAll('div[role="menuitemradio"], div.fxNQSd').length > 0;

                        if (style.display !== 'none' && style.visibility !== 'hidden' && hasMenuItems) {
                            return true;
                        }
                    }
                    return false;
                })();
                """
                position_detected = driver.execute_script(position_check)
                if position_detected:
                    return True
            except Exception:
                pass

            return False

        except Exception as e:
            log.debug(f"Error checking menu state: {e}")
            return False

    def scrape(self):
        """
        Public scrape entry point.

        Wraps `_scrape_once()` with retry-on-session-death (issue #20).
        On `_DriverSessionLost`, already-captured reviews are preserved in
        SQLite (upsert is idempotent by `(review_id, place_id)`), the session
        is marked `partial`, and a fresh driver is launched to retry.
        """
        resilience = self.config.get("resilience", {}) or {}
        max_retries = int(resilience.get("retry_on_session_death", 1))
        backoff_base = int(resilience.get("retry_backoff_base_seconds", 3))

        for attempt in range(max_retries + 1):
            try:
                return self._scrape_once()
            except _DriverSessionLost as e:
                if attempt >= max_retries:
                    log.error(
                        "Driver session lost, retries exhausted (%d): %s",
                        max_retries, e,
                    )
                    return False
                delay = backoff_base * (3 ** attempt)
                log.warning(
                    "Driver session lost (attempt %d/%d) — retrying in %ds: %s",
                    attempt + 1, max_retries + 1, delay, e,
                )
                time.sleep(delay)
            except _RateLimited as e:
                cooldown = int(resilience.get("rate_limit_cooldown_seconds", 60))
                log.warning(
                    "Rate-limit signal detected: %s. Sleeping %ds then aborting "
                    "this scrape (safe to retry later).",
                    e, cooldown,
                )
                time.sleep(cooldown)
                return False
            except InterruptedError:
                log.info("Scrape cancelled — not retrying")
                return False
        return False

    def _scrape_once(self):
        """Single scrape attempt — may raise _DriverSessionLost for retry."""
        start_time = time.time()

        url = self.config.get("url")
        headless = self.config.get("headless", True)
        sort_by = self.config.get("sort_by", "relevance")
        stop_threshold = self.config.get("stop_threshold", 3)
        max_reviews = self.config.get("max_reviews", 0)
        max_scroll_attempts = self.config.get("max_scroll_attempts", 50)
        scroll_idle_limit = self.config.get("scroll_idle_limit", 15)

        # Date filter — early_stop mode requires sort_by=newest (enforced later).
        date_filter = DateFilter(self.config)
        past_boundary_streak = 0

        log.info(f"Starting scraper with settings: headless={headless}, sort_by={sort_by}")
        log.info(f"URL: {url}")

        place_id = None
        session_id = None
        batch_stats = {"new": 0, "updated": 0, "restored": 0, "unchanged": 0}
        changed_ids = set()  # Track IDs that actually changed for efficient sync
        # Profile support: keep scrolling past max_reviews until this many
        # review-attached photos have been seen (0 = disabled).
        min_review_photos = int(self.config.get("min_review_photos", 0) or 0)
        # Ceiling on how far the photo target may push past max_reviews.
        max_reviews_cap = int(self.config.get("max_reviews_cap", 0) or 0)
        review_photos_seen = 0

        driver = None
        try:
            driver = self.setup_driver(headless)
            wait = WebDriverWait(driver, 20)  # Reduced from 40 to 20 for faster timeout

            if self.config.get("extended_warmup"):
                self._extended_warmup(driver)

            # Navigate using limited-view bypass (search-based navigation)
            self._report("navigating")
            self.navigate_to_place(driver, url, wait)

            # Identity of the business we are about to scrape. Every later
            # reload is checked against this, so a reload that lands on some
            # other place cannot file its data under this one.
            expected_ftid = self._current_ftid(driver)
            log.info("Place identity: %s", expected_ftid or "(çözülemedi)")

            # Extract place ID and register in database
            resolved_url = driver.current_url
            place_name = ""
            try:
                title = driver.title or ""
                place_name = title.replace(" - Google Maps", "").strip()
            except Exception:
                pass
            place_id = extract_place_id(url, resolved_url)
            lat, lng = self._extract_place_coords(resolved_url)
            lat_f = float(lat) if lat else None
            lng_f = float(lng) if lng else None
            place_id = self.review_db.upsert_place(
                place_id, place_name, url, resolved_url, lat_f, lng_f
            )
            session_id = self.review_db.start_session(place_id, sort_by)
            log.info(f"Registered place: {place_id} ({place_name})")
            self._selector_health = SelectorHealth(self.review_db.backend, session_id)

            # Load seen IDs from DB (empty for full mode to re-process everything)
            if self.scrape_mode == "full":
                seen = set()
            else:
                seen = self.review_db.get_review_ids(place_id)

            self.dismiss_cookies(driver)

            # Reviews come FIRST. Place details are extracted afterwards (see
            # the call below the review loop) because the owner-photo gallery
            # opens an overlay whose exit is not guaranteed — running it before
            # the reviews used to strand the browser on a photo page and cost
            # the entire review scrape.
            #
            # STAGE: reviews tab. Verified, because a click that silently does
            # nothing leaves us on Overview — whose pane matches the same CSS
            # as the reviews pane, so the scrape then scrolls an empty list
            # instead of reporting a problem.
            # A place with no reviews is not a failed scrape. Maps gives it
            # Overview and About and no Reviews tab, so demanding one throws
            # away the info, photos and about sections that ARE there.
            self._report("reviews_tab")
            availability = self._review_availability(driver)
            has_reviews = availability == "has"
            if availability == "none":
                log.info("Place has no reviews — skipping the review stage")
                reviews_tab_count = 0
            elif availability == "withheld":
                self.capture_failure(driver, "reviews_withheld",
                                     place=place_name, place_id=place_id,
                                     rating=self._rating_block_text(driver))
                self._flag("reviews_withheld",
                           "puan var ama yorum sayısı ve sekmesi yok — "
                           "yorumlar bu adrese gönderilmiyor", place=place_name)
                raise RuntimeError(
                    f"{place_name or place_id}: rating shown without a review "
                    "count and no reviews tab — the reviews were not served to "
                    "this client")
            elif availability == "unknown":
                # Could not prove either way. Failing here costs one retry;
                # guessing "no reviews" would file an empty package as a
                # complete one, and nothing downstream could tell.
                self.capture_failure(driver, "reviews_undetermined",
                                     place=place_name, place_id=place_id)
                self._flag("reviews_undetermined",
                           "sayfadan yorum durumu okunamadı", place=place_name)
                raise RuntimeError(
                    f"could not determine whether {place_name or place_id} has "
                    "reviews (place panel did not render readably)")
            elif not self._ensure_reviews_tab(driver):
                self.capture_failure(driver, "reviews_tab_unreachable",
                                     place=place_name, place_id=place_id)
                self._flag("reviews_tab_unreachable",
                           "yorumlar sekmesi açılamadı", place=place_name)
                raise RuntimeError(
                    f"could not reach the reviews list for {place_name or place_id} "
                    "(reviews tab never became active)")

            try:
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
                log.info("Page DOM is ready")
            except Exception:
                log.debug("Could not verify page ready state")

            # Total review count as shown on the Reviews tab. Kept aside and
            # handed to the details step below, which now runs after this tab
            # is gone (it fills in when the Overview header lacks the count).
            if has_reviews:
                reviews_tab_count = None
            if has_reviews and self.config.get("scrape_place_details", True):
                try:
                    reviews_tab_count = extract_review_count_on_reviews_tab(driver)
                except Exception:  # noqa: BLE001
                    log.debug("Reviews-tab count read failed", exc_info=True)

            # Everything below collects reviews. A place without any has
            # no list to open, no pane to scroll and nothing to sort — the
            # details step after this block is the whole job for it.
            if has_reviews:
                # Verify we're on a reviews page before proceeding
                if "review" not in driver.current_url.lower():
                    log.warning("URL doesn't contain 'review' - might not be on reviews page")

                # Try to set sort - but don't fail if it doesn't work
                sort_ok = False
                try:
                    sort_ok = bool(self.set_sort(driver, sort_by))
                except Exception as sort_error:
                    log.warning(f"Sort failed but continuing: {sort_error}")

                # Early-stop only makes sense when reviews are sorted by newest.
                # If sort failed or sort_by isn't "newest", disable it.
                if stop_threshold > 0 and (not sort_ok or sort_by != "newest"):
                    log.warning(
                        "Disabling early stop (stop_threshold=%d) — "
                        "reviews are not confirmed sorted by newest",
                        stop_threshold,
                    )
                    stop_threshold = 0

                # Add a longer wait after setting sort to allow results to load
                log.info("Waiting for reviews to render...")
                time.sleep(3)

                # Use try-except to handle cases where the pane is not found
                # Try multiple selectors for the reviews pane
                pane = None
                pane_selectors = [
                    PANE_SEL,  # Primary selector
                    'div[role="main"] div.m6QErb',  # Simplified version
                    'div.m6QErb.DxyBCb',  # Even more simplified
                    'div[role="main"]'  # Most generic
                ]

                for selector in pane_selectors:
                    try:
                        log.info(f"Trying to find reviews pane with selector: {selector}")
                        pane = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
                        if pane:
                            log.info(f"Found reviews pane with selector: {selector}")
                            break
                    except TimeoutException:
                        log.debug(f"Pane not found with selector: {selector}")
                        continue

                if not pane:
                    log.warning("Could not find reviews pane with any selector. Page structure might have changed.")
                    return False

                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    transient=False,
                )
                progress.start()
                task_id = progress.add_task("Scraped", total=None, completed=len(seen))
                self._report("reviews", done=len(seen), total=max_reviews or None,
                             place=place_name)
                idle = 0
                processed_ids = set()
                consecutive_matched_batches = 0

                # Prefetch selector to avoid repeated lookups
                try:
                    driver.execute_script("window.scrollablePane = arguments[0];", pane)
                    scroll_script = "window.scrollablePane.scrollBy(0, window.scrollablePane.scrollHeight);"
                except Exception as e:
                    log.warning(f"Error setting up scroll script: {e}")
                    scroll_script = "window.scrollBy(0, 300);"  # Fallback to simple scrolling

                max_attempts = max_scroll_attempts
                attempts = 0
                max_idle = scroll_idle_limit
                consecutive_no_cards = 0  # Track how many times we find zero cards
                last_scroll_position = 0
                scroll_stuck_count = 0

                while attempts < max_attempts:
                    if self.cancel_event.is_set():
                        log.info("Scrape cancelled by user request")
                        raise InterruptedError("Scrape cancelled")

                    # Driver session probe — detects Chrome crashes before the
                    # next find_elements() raises a cryptic error (issue #20).
                    try:
                        driver.execute_script("return 1")
                    except (InvalidSessionIdException, NoSuchWindowException,
                            WebDriverException) as probe_err:
                        raise _DriverSessionLost(str(probe_err)) from probe_err

                    # Rate-limit / CAPTCHA probe. Google routes rate-limited
                    # clients to /sorry/ or shows a reCAPTCHA interstitial.
                    # Either signal means we should cool down instead of
                    # continuing to scroll.
                    try:
                        current_url = (driver.current_url or "").lower()
                        if (
                            "/sorry/" in current_url
                            or "recaptcha" in current_url
                            or "captcha" in current_url
                        ):
                            raise _RateLimited(
                                f"rate-limit redirect detected: {current_url}"
                            )
                    except WebDriverException:
                        # Session already dead — the probe above will surface it
                        # on the next iteration. Don't double-report.
                        pass

                    try:
                        cards = pane.find_elements(By.CSS_SELECTOR, CARD_SEL)
                        fresh_cards: List[WebElement] = []

                        # Check for valid cards
                        if len(cards) == 0:
                            consecutive_no_cards += 1
                            log.info(f"No review cards found in this iteration (consecutive: {consecutive_no_cards})")

                            # If we keep finding no cards, might have hit the end
                            if consecutive_no_cards > 5:
                                log.warning("No cards found for 5+ iterations - might be at end of reviews")
                                break

                            attempts += 1
                            # Try aggressive scrolling
                            driver.execute_script(scroll_script)
                            time.sleep(1)
                            driver.execute_script("window.scrollBy(0, 1000);")  # Extra scroll
                            time.sleep(1.5)
                            continue
                        else:
                            consecutive_no_cards = 0  # Reset counter when we find cards

                        batch_seen_count = 0  # Cards already in DB (for batch stop)
                        for c in cards:
                            try:
                                cid = c.get_attribute("data-review-id")
                                if not cid or cid in processed_ids:
                                    continue
                                processed_ids.add(cid)
                                if cid in seen:
                                    batch_seen_count += 1
                                    continue
                                fresh_cards.append(c)
                            except StaleElementReferenceException:
                                continue
                            except Exception as e:
                                log.debug(f"Error getting review ID: {e}")
                                continue

                        batch_total = len(fresh_cards) + batch_seen_count
                        batch_unchanged = batch_seen_count

                        for card in fresh_cards:
                            try:
                                raw = RawReview.from_card(card)
                            except StaleElementReferenceException:
                                continue
                            except Exception:
                                # Skip the card — do not store empty stubs.
                                # Earlier behavior stored a zero-rating placeholder,
                                # which polluted content hashes and downstream data.
                                batch_stats["parse_errors"] = batch_stats.get("parse_errors", 0) + 1
                                log.warning(
                                    "parse error - skipping card\n%s",
                                    traceback.format_exc(limit=1).strip(),
                                )
                                continue

                            if not raw.id:
                                batch_stats["parse_errors"] = batch_stats.get("parse_errors", 0) + 1
                                continue

                            review_dict = {
                                "review_id": raw.id,
                                "text": raw.text,
                                "rating": raw.rating,
                                "likes": raw.likes,
                                "lang": raw.lang,
                                "date": raw.date,
                                "review_date": raw.review_date,
                                "author": raw.author,
                                "profile": raw.profile,
                                "avatar": raw.avatar,
                                "owner_text": raw.owner_text,
                                "photos": raw.photos,
                                "sub_ratings": raw.sub_ratings,
                            }
                            result = self.review_db.upsert_review(
                                place_id, review_dict, session_id,
                                scrape_mode=self.scrape_mode,
                            )
                            batch_stats[result] = batch_stats.get(result, 0) + 1
                            if result != "unchanged":
                                changed_ids.add(raw.id)
                            if result == "unchanged":
                                batch_unchanged += 1
                            seen.add(raw.id)
                            progress.advance(task_id)
                            idle = 0
                            attempts = 0

                            review_photos_seen += len(raw.photos or [])

                            if max_reviews > 0 and len(seen) >= max_reviews:
                                if (min_review_photos > 0
                                        and review_photos_seen < min_review_photos
                                        and (max_reviews_cap <= 0
                                             or len(seen) < max_reviews_cap)):
                                    # profile wants more review photos — keep going
                                    pass
                                else:
                                    if (max_reviews_cap > 0
                                            and len(seen) >= max_reviews_cap
                                            and review_photos_seen < min_review_photos):
                                        # The photo target is a reason to read on,
                                        # not a blank cheque: a place whose reviews
                                        # carry few photos would otherwise be read
                                        # to the end (1500 reviews for 42 photos).
                                        log.info(
                                            "Hit the review cap (%d) with %d/%d photos, stopping.",
                                            max_reviews_cap, review_photos_seen,
                                            min_review_photos)
                                    else:
                                        log.info(
                                            "Reached max_reviews limit (%d, photos seen: %d), stopping.",
                                            max_reviews, review_photos_seen)
                                    idle = 999
                                    break

                            # Date-filter early-stop (issue #19). Only meaningful
                            # when sort_by is newest AND the user asked for
                            # mode=early_stop with an `after` boundary.
                            if date_filter.early_stop_enabled and sort_by == "newest":
                                if date_filter.is_past_boundary(raw.review_date):
                                    past_boundary_streak += 1
                                    if past_boundary_streak >= EARLY_STOP_CONSECUTIVE:
                                        log.info(
                                            "Date-filter early stop: %d consecutive "
                                            "cards older than %s — ending scrape",
                                            past_boundary_streak, date_filter.raw_after,
                                        )
                                        idle = 999
                                        break
                                else:
                                    past_boundary_streak = 0

                        # Batch-level stop: entire scroll iteration was unchanged.
                        # Require min 3 reviews in the batch to avoid false stops
                        # from tiny tail batches during lazy loading.
                        if stop_threshold > 0 and batch_total >= 3:
                            if batch_unchanged == batch_total:
                                consecutive_matched_batches += 1
                                log.info("Fully matched batch %d/%d (%d reviews)",
                                         consecutive_matched_batches, stop_threshold, batch_total)
                                if consecutive_matched_batches >= stop_threshold:
                                    log.info("Stopping: %d consecutive fully-matched batches",
                                             stop_threshold)
                                    idle = 999
                            else:
                                consecutive_matched_batches = 0

                        if idle >= max_idle:
                            log.info(f"Stopping: No new reviews found after {max_idle} scroll attempts")
                            break

                        if not fresh_cards:
                            idle += 1
                            attempts += 1
                            log.info(f"No new reviews in this iteration (idle: {idle}/{max_idle}, attempts: {attempts}/{max_attempts}, total seen: {len(seen)})")

                            # When no new reviews, scroll more aggressively
                            try:
                                # Try multiple scroll methods
                                driver.execute_script(scroll_script)
                                time.sleep(0.5)
                                driver.execute_script("window.scrollBy(0, 500);")  # Extra scroll
                                time.sleep(0.5)
                            except Exception as e:
                                log.warning(f"Error scrolling: {e}")
                        else:
                            log.info(f"Found {len(fresh_cards)} new reviews in this iteration")
                            self._report("reviews", done=len(seen),
                                         total=max_reviews or None,
                                         photos=review_photos_seen,
                                         photos_target=min_review_photos or None)

                        # Check if we're actually scrolling or stuck
                        try:
                            current_scroll = driver.execute_script("return arguments[0].scrollTop;", pane)
                            if current_scroll == last_scroll_position and len(fresh_cards) == 0:
                                scroll_stuck_count += 1
                                log.warning(f"Scroll position hasn't changed (stuck at {current_scroll}px, stuck count: {scroll_stuck_count})")

                                if scroll_stuck_count > 5:
                                    log.warning("Scroll is stuck - trying alternative scroll method")
                                    # Try clicking the last visible review to force loading
                                    try:
                                        driver.execute_script("arguments[0].lastElementChild.scrollIntoView();", pane)
                                        time.sleep(2)
                                    except Exception:
                                        pass
                                    scroll_stuck_count = 0
                            else:
                                scroll_stuck_count = 0
                                last_scroll_position = current_scroll
                        except Exception:
                            pass

                        # Use JavaScript for smoother scrolling
                        try:
                            driver.execute_script(scroll_script)
                        except Exception as e:
                            log.warning(f"Error scrolling: {e}")
                            # Try a simpler scroll method
                            driver.execute_script("window.scrollBy(0, 300);")

                        # Dynamic sleep: sleep less when processing many reviews, more when finding none
                        if len(fresh_cards) > 5:
                            sleep_time = 0.7
                        elif len(fresh_cards) == 0:
                            sleep_time = 2.0  # Wait longer when finding nothing (let page load)
                        else:
                            sleep_time = 1.0
                        time.sleep(sleep_time)

                    except StaleElementReferenceException:
                        # The pane or other element went stale, try to re-find
                        log.debug("Stale element encountered, re-finding elements")
                        try:
                            pane = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, PANE_SEL)))
                            driver.execute_script("window.scrollablePane = arguments[0];", pane)
                        except Exception:
                            log.warning("Could not re-find reviews pane after stale element")
                            break
                    except Exception as e:
                        log.warning(f"Error during review processing: {e}")
                        attempts += 1
                        time.sleep(1)

                progress.stop()

            # STAGE CHECK: did we get what the page said was there?
            # Maps states the total; stopping short of it without being told
            # to (max_reviews / date filter) means the list stopped yielding,
            # which is a scrape problem, not a small business.
            got = len(seen)
            expected = reviews_tab_count
            if isinstance(expected, int) and expected > 0:
                capped = max_reviews if max_reviews > 0 else expected
                target = min(expected, capped)
                if got == 0:
                    # The package still builds (details are there), so the
                    # coordinator catches this later — by which time the VM is
                    # gone. Capture here, while the page is still on screen:
                    # a place advertising thousands of reviews that yields
                    # none is the failure most worth seeing.
                    self.capture_failure(driver, "no_reviews_collected",
                                         place=place_name, expected=expected)
                    self._flag("no_reviews",
                               "Maps yorum sayısı bildiriyor ama hiç yorum alınamadı",
                               expected=expected, got=0)
                elif got < target * 0.8:
                    self._flag("short_reviews",
                               "yorumlar hedefe ulaşmadan kesildi",
                               expected=expected, target=target, got=got)

            # Reviews are safely in the DB — now (and only now) go back to the
            # place page for the business details. The owner-photo gallery runs
            # last inside this call, so a failure to exit its overlay can no
            # longer cost anything.
            if self.config.get("scrape_place_details", True):
                self._extract_and_store_place_details(
                    driver, place_id,
                    place_url=url,          # original place_id URL: what the
                                            # limited-view bypass expects
                    review_count_fallback=reviews_tab_count,
                    expected_ftid=expected_ftid,
                )

            # End session with stats
            total_found = sum(batch_stats.values())
            parse_errors = batch_stats.get("parse_errors", 0)
            real_found = total_found - parse_errors
            if session_id:
                # Session status: "empty" if zero reviews extracted,
                # "degraded" if >30% of cards failed parsing, else "completed".
                if real_found == 0:
                    session_status = "empty"
                elif total_found and (parse_errors / total_found) > 0.30:
                    session_status = "degraded"
                else:
                    session_status = "completed"
                self.review_db.end_session(
                    session_id, session_status,
                    reviews_found=real_found,
                    reviews_new=batch_stats.get("new", 0),
                    reviews_updated=(
                        batch_stats.get("updated", 0)
                        + batch_stats.get("restored", 0)
                    ),
                )

            # Post-scrape pipeline: process once, write to all targets.
            # Capture browser cookies BEFORE quitting the driver — the image
            # downloader needs them to fetch newer geougc-cs/ABOP... URLs
            # (older AMG... URLs work without cookies). See image_handler.
            browser_cookies = []
            try:
                browser_cookies = driver.get_cookies()
            except Exception:  # noqa: BLE001
                log.debug("Could not extract browser cookies", exc_info=True)

            reviews = self.review_db.get_reviews(place_id) if place_id else []
            if reviews:
                legacy_docs = {
                    r["review_id"]: self._db_review_to_legacy(r) for r in reviews
                }
                self._report("post", step="yorum fotoğrafları / kayıt",
                             total=len(reviews))
                runner = PostScrapeRunner(self.config)
                if browser_cookies:
                    runner.set_browser_cookies(browser_cookies)
                # Scope image/S3/MongoDB tasks to reviews that actually
                # changed this session — avoids repeatedly re-downloading
                # images and re-syncing identical documents. Unchanged
                # reviews already have their images + Mongo docs in place.
                runner.set_changed_ids(changed_ids)
                try:
                    runner.run(legacy_docs, place_id, seen=seen)
                finally:
                    runner.close()

            if self._selector_health is not None:
                self._selector_health.flush()

            log.info(
                "Finished - new: %d, updated: %d, restored: %d, unchanged: %d",
                batch_stats["new"], batch_stats["updated"],
                batch_stats["restored"], batch_stats["unchanged"],
            )
            if batch_stats.get("parse_errors"):
                log.warning(
                    "Parse errors: %d cards skipped due to parser exceptions",
                    batch_stats["parse_errors"],
                )
            log.info("Total unique reviews in DB: %d", len(reviews))

            end_time = time.time()
            elapsed_time = end_time - start_time
            log.info(f"Execution completed in {elapsed_time:.2f} seconds")

            return True

        except _DriverSessionLost:
            # Flush partial session data — upsert is idempotent so the
            # retry attempt will continue where this one left off.
            if session_id:
                try:
                    self.review_db.end_session(
                        session_id, "partial", error="driver session lost",
                    )
                except Exception:  # noqa: BLE001
                    log.debug("Failed to end session on driver loss", exc_info=True)
            if self._selector_health is not None:
                try:
                    self._selector_health.flush()
                except Exception:  # noqa: BLE001
                    pass
            raise

        except _RateLimited as e:
            if session_id:
                try:
                    self.review_db.end_session(
                        session_id, "rate_limited", error=str(e),
                    )
                except Exception:  # noqa: BLE001
                    log.debug("Failed to end session on rate limit", exc_info=True)
            if self._selector_health is not None:
                try:
                    self._selector_health.flush()
                except Exception:  # noqa: BLE001
                    pass
            raise

        except InterruptedError:
            if session_id:
                try:
                    self.review_db.end_session(session_id, "cancelled")
                except Exception:  # noqa: BLE001
                    pass
            raise

        except Exception as e:
            if session_id:
                try:
                    self.review_db.end_session(session_id, "failed", error=str(e))
                except Exception:  # noqa: BLE001
                    pass
            log.error(f"Error during scraping: {e}")
            log.error(traceback.format_exc())
            return False

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

# """
# Selenium scraping logic for Google Maps Reviews.
# """
#
# import os
# import time
# import logging
# import traceback
# import platform
# from typing import Dict, Any, List
#
# import undetected_chromedriver as uc
# from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
# from selenium.webdriver import Chrome
# from selenium.webdriver.common.by import By
# from selenium.webdriver.remote.webelement import WebElement
# from selenium.webdriver.support import expected_conditions as EC
# from selenium.webdriver.support.ui import WebDriverWait
# from tqdm import tqdm
#
# from workers.engine.models import RawReview
# from workers.engine.data_storage import MongoDBStorage, JSONStorage, merge_review
#
# # Logger
# log = logging.getLogger("scraper")
#
# # CSS Selectors
# PANE_SEL = 'div[role="main"] div.m6QErb.DxyBCb.kA9KIf.dS8AEf'
# CARD_SEL = "div[data-review-id]"
# COOKIE_BTN = ('button[aria-label*="Accept" i],'
#               'button[jsname="hZCF7e"],'
#               'button[data-mdc-dialog-action="accept"]')
# SORT_BTN = 'button[aria-label="Sort reviews" i], button[aria-label="Sort" i]'
# MENU_ITEMS = 'div[role="menu"] [role="menuitem"], li[role="menuitem"]'
#
# SORT_LABELS = {  # text shown in Google Maps' menu
#     "newest": ("Newest", "החדשות ביותר", "ใหม่ที่สุด"),
#     "highest": ("Highest rating", "הדירוג הגבוה ביותר", "คะแนนสูงสุด"),
#     "lowest": ("Lowest rating", "הדירוג הנמוך ביותר", "คะแนนต่ำสุด"),
#     "relevance": ("Most relevant", "רלוונטיות ביותר", "เกี่ยวข้องมากที่สุด"),
# }
#
# REVIEW_WORDS = {"reviews", "review", "ביקורות", "รีวิว", "avis", "reseñas",
#                 "recensioni", "bewertungen", "口コミ", "レビュー",
#                 "리뷰", "評論", "评论", "рецензии", "ביקורת"}
#
#
# class GoogleReviewsScraper:
#     """Main scraper class for Google Maps reviews"""
#
#     def __init__(self, config: Dict[str, Any]):
#         """Initialize scraper with configuration"""
#         self.config = config
#         self.use_mongodb = config.get("use_mongodb", True)
#         self.mongodb = MongoDBStorage(config) if self.use_mongodb else None
#         self.json_storage = JSONStorage(config)
#         self.backup_to_json = config.get("backup_to_json", True)
#         self.overwrite_existing = config.get("overwrite_existing", False)
#
#     def setup_driver(self, headless: bool) -> Chrome:
#         """
#         Set up and configure Chrome driver with flexibility for different environments.
#         Works in both Docker containers and on regular OS installations (Windows, Mac, Linux).
#         """
#         # Determine if we're running in a container
#         in_container = os.environ.get('CHROME_BIN') is not None
#
#         # Create Chrome options
#         opts = uc.ChromeOptions()
#         opts.add_argument("--window-size=1400,900")
#         opts.add_argument("--ignore-certificate-errors")
#         opts.add_argument("--disable-gpu")  # Improves performance
#         opts.add_argument("--disable-dev-shm-usage")  # Helps with stability
#         opts.add_argument("--no-sandbox")  # More stable in some environments
#
#         # Use headless mode if requested
#         if headless:
#             opts.add_argument("--headless=new")
#
#         # Log platform information for debugging
#         log.info(f"Platform: {platform.platform()}")
#         log.info(f"Python version: {platform.python_version()}")
#
#         # If in container, use environment-provided binaries
#         if in_container:
#             chrome_binary = os.environ.get('CHROME_BIN')
#             chromedriver_path = os.environ.get('CHROMEDRIVER_PATH')
#
#             log.info(f"Container environment detected")
#             log.info(f"Chrome binary: {chrome_binary}")
#             log.info(f"ChromeDriver path: {chromedriver_path}")
#
#             if chrome_binary and os.path.exists(chrome_binary):
#                 log.info(f"Using Chrome binary from environment: {chrome_binary}")
#                 opts.binary_location = chrome_binary
#
#             try:
#                 # Try creating Chrome driver with undetected_chromedriver
#                 log.info("Attempting to create undetected_chromedriver instance")
#                 driver = uc.Chrome(options=opts)
#                 log.info("Successfully created undetected_chromedriver instance")
#             except Exception as e:
#                 # Fall back to regular Selenium if undetected_chromedriver fails
#                 log.warning(f"Failed to create undetected_chromedriver instance: {e}")
#                 log.info("Falling back to regular Selenium Chrome")
#
#                 # Import Selenium webdriver here to avoid potential import issues
#                 from selenium import webdriver
#                 from selenium.webdriver.chrome.service import Service
#
#                 if chromedriver_path and os.path.exists(chromedriver_path):
#                     log.info(f"Using ChromeDriver from path: {chromedriver_path}")
#                     service = Service(executable_path=chromedriver_path)
#                     driver = webdriver.Chrome(service=service, options=opts)
#                 else:
#                     log.info("Using default ChromeDriver")
#                     driver = webdriver.Chrome(options=opts)
#         else:
#             # On regular OS, use default undetected_chromedriver
#             log.info("Using standard undetected_chromedriver setup")
#             driver = uc.Chrome(options=opts)
#
#         # Set page load timeout to avoid hanging
#         driver.set_page_load_timeout(30)
#         log.info("Chrome driver setup completed successfully")
#         return driver
#
#     def dismiss_cookies(self, driver: Chrome):
#         """
#         Dismiss cookie consent dialogs if present.
#         Handles stale element references by re-finding elements if needed.
#         """
#         try:
#             # Use WebDriverWait with expected_conditions to handle stale elements
#             WebDriverWait(driver, 3).until(
#                 EC.presence_of_element_located((By.CSS_SELECTOR, COOKIE_BTN))
#             )
#             log.info("Cookie consent dialog found, attempting to dismiss")
#
#             # Get elements again after waiting to avoid stale references
#             elements = driver.find_elements(By.CSS_SELECTOR, COOKIE_BTN)
#             for elem in elements:
#                 try:
#                     if elem.is_displayed():
#                         elem.click()
#                         log.info("Cookie dialog dismissed")
#                         return True
#                 except Exception as e:
#                     log.debug(f"Error clicking cookie button: {e}")
#                     continue
#         except TimeoutException:
#             # This is expected if no cookie dialog is present
#             log.debug("No cookie consent dialog detected")
#         except Exception as e:
#             log.debug(f"Error handling cookie dialog: {e}")
#
#         return False
#
#     def is_reviews_tab(self, tab: WebElement) -> bool:
#         """Check if a tab is the reviews tab"""
#         try:
#             label = (tab.get_attribute("aria-label") or tab.text or "").lower()
#             return tab.get_attribute("data-tab-index") == "1" or any(w in label for w in REVIEW_WORDS)
#         except StaleElementReferenceException:
#             return False
#         except Exception as e:
#             log.debug(f"Error checking if tab is reviews tab: {e}")
#             return False
#
#     def click_reviews_tab(self, driver: Chrome):
#         """
#         Click on the reviews tab in Google Maps with improved stale element handling.
#         """
#         end = time.time() + 15  # Timeout after 15 seconds
#         while time.time() < end:
#             try:
#                 # Find all tab elements
#                 tabs = driver.find_elements(By.CSS_SELECTOR, '[role="tab"], button[aria-label]')
#
#                 for tab in tabs:
#                     try:
#                         # Check if this is the reviews tab
#                         label = (tab.get_attribute("aria-label") or tab.text or "").lower()
#                         is_review_tab = tab.get_attribute("data-tab-index") == "1" or any(
#                             w in label for w in REVIEW_WORDS)
#
#                         if is_review_tab:
#                             # Scroll the tab into view
#                             driver.execute_script("arguments[0].scrollIntoView({block:\"center\"});", tab)
#                             time.sleep(0.2)  # Small wait after scrolling
#
#                             # Try to click the tab
#                             log.info("Found reviews tab, attempting to click")
#                             tab.click()
#                             log.info("Successfully clicked reviews tab")
#                             return True
#                     except Exception as e:
#                         # Element might be stale or not clickable, try the next one
#                         log.debug(f"Error with tab element: {str(e)}")
#                         continue
#
#                 # If we get here, we didn't find a suitable tab in this iteration
#                 log.debug("No reviews tab found in this iteration, waiting...")
#                 time.sleep(0.5)  # Wait before next attempt
#
#             except Exception as e:
#                 # General exception handling
#                 log.debug(f"Exception while looking for reviews tab: {str(e)}")
#                 time.sleep(0.5)
#
#         # If we exit the loop, we've timed out
#         log.warning("Timeout while looking for reviews tab")
#         raise TimeoutException("Reviews tab not found")
#
#     def set_sort(self, driver: Chrome, method: str):
#         """
#         Set the sorting method for reviews with improved error handling.
#         """
#         if method == "relevance":
#             return True  # Default order, no need to change
#
#         log.info(f"Attempting to set sort order to '{method}'")
#
#         try:
#             # First try to find and click the sort button
#             sort_buttons = driver.find_elements(By.CSS_SELECTOR, SORT_BTN)
#             if not sort_buttons:
#                 log.warning(f"Sort button not found - keeping default sort order")
#                 return False
#
#             # Try to click the first visible sort button
#             for sort_button in sort_buttons:
#                 try:
#                     if sort_button.is_displayed() and sort_button.is_enabled():
#                         sort_button.click()
#                         log.info("Clicked sort button")
#                         time.sleep(0.5)  # Wait for menu to appear
#                         break
#                 except Exception as e:
#                     log.debug(f"Error clicking sort button: {e}")
#                     continue
#             else:
#                 log.warning("No clickable sort button found")
#                 return False
#
#             # Now find and click the menu item for the desired sort method
#             wanted = SORT_LABELS[method]
#             menu_items = WebDriverWait(driver, 3).until(
#                 EC.presence_of_all_elements_located((By.CSS_SELECTOR, MENU_ITEMS))
#             )
#
#             for item in menu_items:
#                 try:
#                     label = item.text.strip()
#                     if label in wanted:
#                         item.click()
#                         log.info(f"Selected sort option: {label}")
#                         time.sleep(0.5)  # Wait for sorting to take effect
#                         return True
#                 except Exception as e:
#                     log.debug(f"Error clicking menu item: {e}")
#                     continue
#
#             log.warning(f"Sort option '{method}' not found in menu - keeping default")
#             return False
#
#         except Exception as e:
#             log.warning(f"Error setting sort order: {e}")
#             return False
#
#     def scrape(self):
#         """Main scraper method"""
#         start_time = time.time()
#
#         url = self.config.get("url")
#         headless = self.config.get("headless", True)
#         sort_by = self.config.get("sort_by", "relevance")
#         stop_on_match = self.config.get("stop_on_match", False)
#
#         log.info(f"Starting scraper with settings: headless={headless}, sort_by={sort_by}")
#         log.info(f"URL: {url}")
#
#         # Initialize storage
#         # If not overwriting, load existing data
#         if self.overwrite_existing:
#             docs = {}
#             seen = set()
#         else:
#             # Try to get from MongoDB first if enabled
#             docs = {}
#             if self.use_mongodb and self.mongodb:
#                 docs = self.mongodb.fetch_existing_reviews()
#
#             # If backup_to_json is enabled, also load from JSON for merging
#             if self.backup_to_json:
#                 json_docs = self.json_storage.load_json_docs()
#                 # Merge JSON docs with MongoDB docs
#                 for review_id, review in json_docs.items():
#                     if review_id not in docs:
#                         docs[review_id] = review
#
#             # Load seen IDs from file
#             seen = self.json_storage.load_seen()
#
#         driver = None
#         try:
#             driver = self.setup_driver(headless)
#             wait = WebDriverWait(driver, 20)  # Reduced from 40 to 20 for faster timeout
#
#             driver.get(url)
#             wait.until(lambda d: "google.com/maps" in d.current_url)
#
#             self.dismiss_cookies(driver)
#             self.click_reviews_tab(driver)
#             self.set_sort(driver, sort_by)
#
#             # Add a wait after setting sort to allow results to load
#             time.sleep(1)
#
#             # Use try-except to handle cases where the pane is not found
#             try:
#                 pane = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, PANE_SEL)))
#             except TimeoutException:
#                 log.warning("Could not find reviews pane. Page structure might have changed.")
#                 return False
#
#             pbar = tqdm(desc="Scraped", ncols=80, initial=len(seen))
#             idle = 0
#             processed_ids = set()  # Track processed IDs in current session
#
#             # Prefetch selector to avoid repeated lookups
#             try:
#                 driver.execute_script("window.scrollablePane = arguments[0];", pane)
#                 scroll_script = "window.scrollablePane.scrollBy(0, window.scrollablePane.scrollHeight);"
#             except Exception as e:
#                 log.warning(f"Error setting up scroll script: {e}")
#                 scroll_script = "window.scrollBy(0, 300);"  # Fallback to simple scrolling
#
#             max_attempts = 10  # Limit the number of attempts to find reviews
#             attempts = 0
#
#             while attempts < max_attempts:
#                 try:
#                     cards = pane.find_elements(By.CSS_SELECTOR, CARD_SEL)
#                     fresh_cards: List[WebElement] = []
#
#                     # Check for valid cards
#                     if len(cards) == 0:
#                         log.debug("No review cards found in this iteration")
#                         attempts += 1
#                         # Try scrolling anyway
#                         driver.execute_script(scroll_script)
#                         time.sleep(1)
#                         continue
#
#                     for c in cards:
#                         try:
#                             cid = c.get_attribute("data-review-id")
#                             if not cid or cid in seen or cid in processed_ids:
#                                 if stop_on_match and cid and (cid in seen or cid in processed_ids):
#                                     idle = 999
#                                     break
#                                 continue
#                             fresh_cards.append(c)
#                         except StaleElementReferenceException:
#                             continue
#                         except Exception as e:
#                             log.debug(f"Error getting review ID: {e}")
#                             continue
#
#                     for card in fresh_cards:
#                         try:
#                             raw = RawReview.from_card(card)
#                             processed_ids.add(raw.id)  # Track this ID to avoid re-processing
#                         except StaleElementReferenceException:
#                             continue
#                         except Exception:
#                             log.warning("⚠️ parse error – storing stub\n%s",
#                                         traceback.format_exc(limit=1).strip())
#                             try:
#                                 raw_id = card.get_attribute("data-review-id") or ""
#                                 raw = RawReview(id=raw_id, text="", lang="und")
#                                 processed_ids.add(raw_id)
#                             except StaleElementReferenceException:
#                                 continue
#
#                         docs[raw.id] = merge_review(docs.get(raw.id), raw)
#                         seen.add(raw.id)
#                         pbar.update(1)
#                         idle = 0
#                         attempts = 0  # Reset attempts counter when we successfully process a review
#
#                     if idle >= 3:
#                         break
#
#                     if not fresh_cards:
#                         idle += 1
#                         attempts += 1
#
#                     # Use JavaScript for smoother scrolling
#                     try:
#                         driver.execute_script(scroll_script)
#                     except Exception as e:
#                         log.warning(f"Error scrolling: {e}")
#                         # Try a simpler scroll method
#                         driver.execute_script("window.scrollBy(0, 300);")
#
#                     # Dynamic sleep: sleep less when processing many reviews
#                     sleep_time = 0.7 if len(fresh_cards) > 5 else 1.0
#                     time.sleep(sleep_time)
#
#                 except StaleElementReferenceException:
#                     # The pane or other element went stale, try to re-find
#                     log.debug("Stale element encountered, re-finding elements")
#                     try:
#                         pane = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, PANE_SEL)))
#                         driver.execute_script("window.scrollablePane = arguments[0];", pane)
#                     except Exception:
#                         log.warning("Could not re-find reviews pane after stale element")
#                         break
#                 except Exception as e:
#                     log.warning(f"Error during review processing: {e}")
#                     attempts += 1
#                     time.sleep(1)
#
#             pbar.close()
#
#             # Save to MongoDB if enabled
#             if self.use_mongodb and self.mongodb:
#                 log.info("Saving reviews to MongoDB...")
#                 self.mongodb.save_reviews(docs)
#
#             # Backup to JSON if enabled
#             if self.backup_to_json:
#                 log.info("Backing up to JSON...")
#                 self.json_storage.save_json_docs(docs)
#                 self.json_storage.save_seen(seen)
#
#             log.info("✅ Finished – total unique reviews: %s", len(docs))
#
#             end_time = time.time()
#             elapsed_time = end_time - start_time
#             log.info(f"Execution completed in {elapsed_time:.2f} seconds")
#
#             return True
#
#         except Exception as e:
#             log.error(f"Error during scraping: {e}")
#             log.error(traceback.format_exc())
#             return False
#
#         finally:
#             if driver is not None:
#                 try:
#                     driver.quit()
#                 except Exception:
#                     pass
#
#             if self.mongodb:
#                 try:
#                     self.mongodb.close()
#                 except Exception:
#                     pass
#
# # """
# # Selenium scraping logic for Google Maps Reviews.
# # """
# #
# # import re
# # import time
# # import logging
# # import traceback
# # from typing import Dict, Any, Set, List
# #
# # import undetected_chromedriver as uc
# # from selenium.common.exceptions import TimeoutException
# # from selenium.webdriver import Chrome
# # from selenium.webdriver.common.by import By
# # from selenium.webdriver.remote.webelement import WebElement
# # from selenium.webdriver.support import expected_conditions as EC
# # from selenium.webdriver.support.ui import WebDriverWait
# # from tqdm import tqdm
# #
# # from workers.engine.models import RawReview
# # from workers.engine.data_storage import MongoDBStorage, JSONStorage, merge_review
# # from workers.engine.utils import click_if
# #
# # # Logger
# # log = logging.getLogger("scraper")
# #
# # # CSS Selectors
# # PANE_SEL = 'div[role="main"] div.m6QErb.DxyBCb.kA9KIf.dS8AEf'
# # CARD_SEL = "div[data-review-id]"
# # COOKIE_BTN = ('button[aria-label*="Accept" i],'
# #               'button[jsname="hZCF7e"],'
# #               'button[data-mdc-dialog-action="accept"]')
# # SORT_BTN = 'button[aria-label="Sort reviews" i], button[aria-label="Sort" i]'
# # MENU_ITEMS = 'div[role="menu"] [role="menuitem"], li[role="menuitem"]'
# #
# # SORT_LABELS = {  # text shown in Google Maps' menu
# #     "newest": ("Newest", "החדשות ביותר", "ใหม่ที่สุด"),
# #     "highest": ("Highest rating", "הדירוג הגבוה ביותר", "คะแนนสูงสุด"),
# #     "lowest": ("Lowest rating", "הדירוג הנמוך ביותר", "คะแนนต่ำสุด"),
# #     "relevance": ("Most relevant", "רלוונטיות ביותר", "เกี่ยวข้องมากที่สุด"),
# # }
# #
# # REVIEW_WORDS = {"reviews", "review", "ביקורות", "รีวิว", "avis", "reseñas",
# #                 "recensioni", "bewertungen", "口コミ", "レビュー",
# #                 "리뷰", "評論", "评论", "рецензии"}
# #
# #
# # class GoogleReviewsScraper:
# #     """Main scraper class for Google Maps reviews"""
# #
# #     def __init__(self, config: Dict[str, Any]):
# #         """Initialize scraper with configuration"""
# #         self.config = config
# #         self.use_mongodb = config.get("use_mongodb", True)
# #         self.mongodb = MongoDBStorage(config) if self.use_mongodb else None
# #         self.json_storage = JSONStorage(config)
# #         self.backup_to_json = config.get("backup_to_json", True)
# #         self.overwrite_existing = config.get("overwrite_existing", False)
# #
# #     def setup_driver(self, headless: bool) -> Chrome:
# #         """Set up and configure Chrome driver"""
# #         opts = uc.ChromeOptions()
# #         opts.add_argument("--window-size=1400,900")
# #         opts.add_argument("--ignore-certificate-errors")
# #         opts.add_argument("--disable-gpu")  # Improves performance
# #         opts.add_argument("--disable-dev-shm-usage")  # Helps with stability
# #         opts.add_argument("--no-sandbox")  # More stable in some environments
# #
# #         if headless:
# #             opts.add_argument("--headless=new")
# #
# #         driver = uc.Chrome(options=opts)
# #         # Set page load timeout to avoid hanging
# #         driver.set_page_load_timeout(30)
# #         return driver
# #
# #     def dismiss_cookies(self, driver: Chrome):
# #         """Dismiss cookie consent dialogs"""
# #         click_if(driver, COOKIE_BTN, timeout=3.0)  # Reduced timeout for faster operation
# #
# #     def is_reviews_tab(self, tab: WebElement) -> bool:
# #         """Check if a tab is the reviews tab"""
# #         label = (tab.get_attribute("aria-label") or tab.text or "").lower()
# #         return tab.get_attribute("data-tab-index") == "1" or any(w in label for w in REVIEW_WORDS)
# #
# #     def click_reviews_tab(self, driver: Chrome):
# #         """Click on the reviews tab in Google Maps"""
# #         end = time.time() + 15  # Reduced timeout from 30 to 15 seconds
# #         while time.time() < end:
# #             for tab in driver.find_elements(By.CSS_SELECTOR,
# #                                             '[role="tab"], button[aria-label]'):
# #                 if self.is_reviews_tab(tab):
# #                     driver.execute_script("arguments[0].scrollIntoView({block:\"center\"});", tab)
# #                     try:
# #                         tab.click()
# #                         return
# #                     except Exception:
# #                         continue
# #             time.sleep(.2)  # Reduced sleep time from 0.4 to 0.2
# #         raise TimeoutException("Reviews tab not found")
# #
# #     def set_sort(self, driver: Chrome, method: str):
# #         """Set the sorting method for reviews"""
# #         if method == "relevance":
# #             return  # default order
# #         if not click_if(driver, SORT_BTN):
# #             return
# #
# #         wanted = SORT_LABELS[method]
# #
# #         for item in driver.find_elements(By.CSS_SELECTOR, MENU_ITEMS):
# #             label = item.text.strip()
# #             if label in wanted:
# #                 item.click()
# #                 time.sleep(0.5)  # Reduced wait time from 1.0 to 0.5
# #                 return
# #         log.warning("⚠️  sort option %s not found – keeping default", method)
# #
# #     def scrape(self):
# #         """Main scraper method"""
# #         start_time = time.time()
# #
# #         url = self.config.get("url")
# #         headless = self.config.get("headless", True)
# #         sort_by = self.config.get("sort_by", "relevance")
# #         stop_on_match = self.config.get("stop_on_match", False)
# #
# #         log.info(f"Starting scraper with settings: headless={headless}, sort_by={sort_by}")
# #         log.info(f"URL: {url}")
# #
# #         # Initialize storage
# #         # If not overwriting, load existing data
# #         if self.overwrite_existing:
# #             docs = {}
# #             seen = set()
# #         else:
# #             # Try to get from MongoDB first if enabled
# #             docs = {}
# #             if self.use_mongodb and self.mongodb:
# #                 docs = self.mongodb.fetch_existing_reviews()
# #
# #             # If backup_to_json is enabled, also load from JSON for merging
# #             if self.backup_to_json:
# #                 json_docs = self.json_storage.load_json_docs()
# #                 # Merge JSON docs with MongoDB docs
# #                 for review_id, review in json_docs.items():
# #                     if review_id not in docs:
# #                         docs[review_id] = review
# #
# #             # Load seen IDs from file
# #             seen = self.json_storage.load_seen()
# #
# #         driver = self.setup_driver(headless)
# #         wait = WebDriverWait(driver, 20)  # Reduced from 40 to 20 for faster timeout
# #
# #         try:
# #             driver.get(url)
# #             wait.until(lambda d: "google.com/maps" in d.current_url)
# #
# #             self.dismiss_cookies(driver)
# #             self.click_reviews_tab(driver)
# #             self.set_sort(driver, sort_by)
# #
# #             pane = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, PANE_SEL)))
# #             pbar = tqdm(desc="Scraped", ncols=80, initial=len(seen))
# #             idle = 0
# #             processed_ids = set()  # Track processed IDs in current session
# #
# #             # Prefetch selector to avoid repeated lookups
# #             driver.execute_script("window.scrollablePane = arguments[0];", pane)
# #             scroll_script = "window.scrollablePane.scrollBy(0, window.scrollablePane.scrollHeight);"
# #
# #             while True:
# #                 cards = pane.find_elements(By.CSS_SELECTOR, CARD_SEL)
# #                 fresh_cards: List[WebElement] = []
# #
# #                 for c in cards:
# #                     cid = c.get_attribute("data-review-id")
# #                     if cid in seen or cid in processed_ids:
# #                         if stop_on_match:
# #                             idle = 999
# #                             break
# #                         continue
# #                     fresh_cards.append(c)
# #
# #                 for card in fresh_cards:
# #                     try:
# #                         raw = RawReview.from_card(card)
# #                         processed_ids.add(raw.id)  # Track this ID to avoid re-processing
# #                     except Exception:
# #                         log.warning("⚠️ parse error – storing stub\n%s",
# #                                     traceback.format_exc(limit=1).strip())
# #                         raw_id = card.get_attribute("data-review-id") or ""
# #                         raw = RawReview(id=raw_id, text="", lang="und")
# #                         processed_ids.add(raw_id)
# #
# #                     docs[raw.id] = merge_review(docs.get(raw.id), raw)
# #                     seen.add(raw.id)
# #                     pbar.update(1)
# #                     idle = 0
# #
# #                 if idle >= 3:
# #                     break
# #
# #                 if not fresh_cards:
# #                     idle += 1
# #
# #                 # Use JavaScript for smoother scrolling
# #                 driver.execute_script(scroll_script)
# #
# #                 # Dynamic sleep: sleep less when processing many reviews
# #                 sleep_time = 0.7 if len(fresh_cards) > 5 else 1.0
# #                 time.sleep(sleep_time)
# #
# #             pbar.close()
# #
# #             # Save to MongoDB if enabled
# #             if self.use_mongodb and self.mongodb:
# #                 log.info("Saving reviews to MongoDB...")
# #                 self.mongodb.save_reviews(docs)
# #
# #             # Backup to JSON if enabled
# #             if self.backup_to_json:
# #                 log.info("Backing up to JSON...")
# #                 self.json_storage.save_json_docs(docs)
# #                 self.json_storage.save_seen(seen)
# #
# #             log.info("✅ Finished – total unique reviews: %s", len(docs))
# #
# #             end_time = time.time()
# #             elapsed_time = end_time - start_time
# #             log.info(f"Execution completed in {elapsed_time:.2f} seconds")
# #
# #         finally:
# #             driver.quit()
# #             if self.mongodb:
# #                 self.mongodb.close()
