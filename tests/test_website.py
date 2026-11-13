"""The project page in site/ (deployed by .github/workflows/pages.yml): both languages exist and point
at each other, the structured data parses and matches what the page shows, and every picture it uses
is one of docs/screenshots, which the workflow copies in."""
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


def test_pictures_come_from_docs_screenshots():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "cp -R docs/screenshots site/screenshots" in wf and "path: site" in wf
    for lang in PAGES:
        p = parse(lang)
        srcs = [i["src"] for i in p.imgs] + [p.metas["og:image"], p.metas["twitter:image"]]
        for src in srcs:
            name = re.sub(r"^(\.\./|https://[^ ]*/screenshots/)", "", src).replace("screenshots/", "")
            assert (ROOT / "docs" / "screenshots" / name).exists(), src
        for i in p.imgs:
            assert i.get("alt") and i.get("width") == "1440" and i.get("height") == "900", i["src"]


def test_google_search_console_file_stays():
    """Google re-checks this file; removing it drops the site's verified ownership in Search Console."""
    f = SITE / "google3ed451f05041f5f9.html"
    assert f.read_text(encoding="utf-8").strip() == "google-site-verification: google3ed451f05041f5f9.html"
