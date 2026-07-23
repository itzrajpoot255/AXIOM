"""Web tools — search, fetch, weather, news. Network only on demand."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, quote_plus, urljoin
from urllib.request import Request, urlopen

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ArcLocal/2.0"


def _http_get(url: str, timeout: float = 15.0, ua: str = _UA) -> str:
    req = Request(url, headers={"User-Agent": ua})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;?", " ", html)
    html = re.sub(r"&amp;?", "&", html)
    return re.sub(r"\s{2,}", " ", html).strip()


def register(r) -> None:

    @r.register("web_search", "Search the web, returns top results with URLs",
                {"query": "string: search terms", "?max_results": "integer: default 5"})
    def web_search(ctx, query: str, max_results: int = 5) -> str:
        if not query.strip():
            return "Empty query."
        n = max(1, min(10, int(max_results or 5)))
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=n))
            if results:
                out = []
                for res in results:
                    out.append(f"- {res.get('title', '?')}\n  {res.get('body', '')[:220]}\n"
                               f"  {res.get('href', '')}")
                return f"Results for '{query}':\n" + "\n".join(out)
        except ImportError:
            pass
        except Exception as e:
            return f"Search failed: {e} (is `ddgs` installed and the network up?)"
        return "Search unavailable — install with `pip install ddgs`."

    @r.register("fetch_url",
                "Fetch/scrape a web page: readable text, all links, or just the "
                "parts matching a CSS selector",
                {"url": "string: page URL",
                 "?selector": "string: CSS selector, e.g. 'table.prices' or 'article h2'",
                 "?links": "boolean: return the page's links instead of its text"})
    def fetch_url(ctx, url: str, selector: str = "", links: bool = False) -> str:
        url = url.strip()
        if not url:
            return "No URL."
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            html = _http_get(url, timeout=20)
        except Exception as e:
            return f"Fetch failed for {url}: {e}"

        if links:
            out = []
            seen = set()
            for href, label in re.findall(
                    r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html,
                    re.IGNORECASE | re.DOTALL):
                target = urljoin(url, href.strip())
                if not target.startswith("http") or target in seen:
                    continue
                seen.add(target)
                text = _strip_html(label)[:80] or "(no text)"
                out.append(f"- {text}: {target}")
                if len(out) >= 80:
                    out.append("...[more links truncated]")
                    break
            return (f"Links on {url}:\n" + "\n".join(out)) if out else f"No links found on {url}."

        if selector.strip():
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                return "CSS selection needs beautifulsoup4 — `pip install beautifulsoup4`."
            soup = BeautifulSoup(html, "html.parser")
            matches = soup.select(selector.strip())
            if not matches:
                return f"Nothing matched '{selector}' on {url}."
            text = "\n---\n".join(
                m.get_text(separator="\n", strip=True) for m in matches[:20])
            if len(text) > 6000:
                text = text[:6000] + "\n...[truncated]"
            return f"{url} [{selector}], {len(matches)} match(es):\n{text}"

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n", strip=True))
        except ImportError:
            text = _strip_html(html)
        if len(text) > 6000:
            text = text[:6000] + "\n...[truncated]"
        return f"{url}:\n{text}"

    @r.register("weather", "Current weather for a city",
                {"?city": "string: city name, default local"})
    def weather(ctx, city: str = "") -> str:
        loc = quote_plus(city.strip()) if city.strip() else ""
        fmt = quote("%l: %C, %t (feels %f), humidity %h, wind %w")
        try:
            # wttr.in serves plain text only to curl-ish user agents
            return _http_get(f"https://wttr.in/{loc}?format={fmt}", timeout=10,
                             ua="curl/8.0").strip()
        except Exception as e:
            return f"Weather lookup failed: {e}"

    @r.register("news_headlines", "Top news headlines",
                {"?limit": "integer: default 5"})
    def news_headlines(ctx, limit: int = 5) -> str:
        n = max(1, min(15, int(limit or 5)))
        cfg = ctx.cfg
        region = (cfg.news_region if cfg else "US") or "US"
        lang = (cfg.news_lang if cfg else "en") or "en"
        url = (f"https://news.google.com/rss?hl={lang}-{region}"
               f"&gl={region}&ceid={region}:{lang}")
        try:
            root = ET.fromstring(_http_get(url))
        except Exception as e:
            return f"News fetch failed: {e}"
        heads = []
        for item in root.findall("./channel/item/title")[:n]:
            text = (item.text or "").strip()
            if " - " in text:
                text = text.rsplit(" - ", 1)[0]
            if text:
                heads.append(text)
        return ("Top headlines:\n" + "\n".join(f"{i}. {h}" for i, h in enumerate(heads, 1))
                if heads else "No headlines found.")
