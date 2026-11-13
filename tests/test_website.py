"""The project page in site/ (deployed by .github/workflows/pages.yml): both languages exist and point
at each other, the structured data parses and matches what the page shows, and its pictures come from
docs/screenshots and its share cards from docs/assets, which the workflow copies in."""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
BASE = "https://jackson-cmd.github.io/zeus-options-terminal/"
PAGES = {"en": SITE / "index.html", "zh": SITE / "zh" / "index.html"}


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.imgs, self.ld, self.summaries, self.metas = [], [], [], [], {}
        self._ld = self._sum = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link":
            self.links.append(a)
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "meta":
            self.metas[a.get("name") or a.get("property")] = a.get("content")
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._ld = ""
        elif tag == "summary":
            self._sum = ""

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ld.append(json.loads(self._ld))
            self._ld = None
        elif tag == "summary" and self._sum is not None:
            self.summaries.append(self._sum.strip())
            self._sum = None

    def handle_data(self, data):
        if self._ld is not None:
            self._ld += data
        if self._sum is not None:
            self._sum += data


def parse(lang):
    p = Page()
    p.feed(PAGES[lang].read_text(encoding="utf-8"))
    return p


def test_both_languages_point_at_each_other():
    for lang, url in (("en", BASE), ("zh", BASE + "zh/")):
        p = parse(lang)
        canon = [l["href"] for l in p.links if l.get("rel") == "canonical"]
        alts = {l["hreflang"]: l["href"] for l in p.links if l.get("rel") == "alternate"}
        assert canon == [url]
        assert alts == {"en": BASE, "zh-Hans": BASE + "zh/", "x-default": BASE}
        assert p.metas["og:url"] == url and p.metas["description"] and len(p.metas["description"]) <= 220
    sitemap = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    assert f"<loc>{BASE}</loc>" in sitemap and f"<loc>{BASE}zh/</loc>" in sitemap


def test_structured_data_matches_the_page():
    for lang in PAGES:
        p = parse(lang)
        types = {d["@type"] for d in p.ld}
        assert types == {"SoftwareApplication", "FAQPage"}
        faq = next(d for d in p.ld if d["@type"] == "FAQPage")
        assert [q["name"] for q in faq["mainEntity"]] == p.summaries      # same questions, same order
        html = PAGES[lang].read_text(encoding="utf-8")
        for q in faq["mainEntity"]:
            assert q["acceptedAnswer"]["text"] in html                    # the answers are on the page too


def test_pictures_come_from_docs():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "cp -R docs/screenshots site/screenshots" in wf and "path: site" in wf
    for lang, card in (("en", "social.png"), ("zh", "social_zh.png")):
        p = parse(lang)
        for i in p.imgs:
            name = re.sub(r"^(\.\./)?screenshots/", "", i["src"])
            assert (ROOT / "docs" / "screenshots" / name).exists(), i["src"]
            assert i.get("alt") and i.get("width") == "1440" and i.get("height") == "900", i["src"]
        # the link-preview card: 1280 x 640, under GitHub's 1 MB limit for a social preview
        assert p.metas["og:image"] == p.metas["twitter:image"] == BASE + card
        assert (p.metas["og:image:width"], p.metas["og:image:height"]) == ("1280", "640") and p.metas["og:image:alt"]
        assert f"cp docs/assets/{card} site/{card}" in wf
        png = (ROOT / "docs" / "assets" / card).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) < 1_000_000
        assert (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")) == (1280, 640)


def test_google_search_console_file_stays():
    """Google re-checks this file; removing it drops the site's verified ownership in Search Console."""
    f = SITE / "google3ed451f05041f5f9.html"
    assert f.read_text(encoding="utf-8").strip() == "google-site-verification: google3ed451f05041f5f9.html"


def test_bing_webmaster_tag_stays():
    """Bing Webmaster Tools re-checks this tag in the English page's head; removing it drops the
    site's verified ownership there."""
    assert parse("en").metas["msvalidate.01"] == "38CA98D01ECA41B71334E56072B086B7"
    html = PAGES["en"].read_text(encoding="utf-8")
    assert html.index('name="msvalidate.01"') < html.index("</head>")


def test_llms_txt_lists_every_page_and_existing_docs():
    """site/llms.txt is the plain-text map that AI search tools read: every page in the sitemap is in
    it, every file it links on GitHub exists, and its docker run stays on this computer."""
    text = (SITE / "llms.txt").read_text(encoding="utf-8")
    assert text.startswith("# ZEUS Terminal\n\n> ")
    pages = re.findall(r"<loc>([^<]+)</loc>", (SITE / "sitemap.xml").read_text(encoding="utf-8"))
    assert pages and all(f"]({u})" in text for u in pages)
    for name in re.findall(r"blob/main/([^)#\s]+)", text):
        assert (ROOT / name).is_file(), f"llms.txt links to a missing file: {name}"
    assert all("-p 127.0.0.1:" in line for line in text.splitlines() if "docker run" in line)


def test_indexnow_key_file_matches_the_workflow():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    key = re.search(r"INDEXNOW_KEY: ([0-9a-f]{32})", wf)[1]
    assert (SITE / f"{key}.txt").read_bytes() == key.encode()          # the key itself, no newline
    assert f"SITE_URL: {BASE}" in wf and "continue-on-error: true" in wf
