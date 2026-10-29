"""Optional observer only: never requests data or changes a provider result."""
from contextvars import ContextVar
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_observer = ContextVar("v2_evidence_observer", default=None)


def observe(provider, payload, *, scope="UNSPECIFIED", symbols=(), state="AVAILABLE", limitations=()):
    callback = _observer.get()
    if callback is None:
        return
    try:
        callback(provider, payload, scope=scope, symbols=symbols, state=state, limitations=limitations)
    except Exception as exc:
        logging.getLogger("market_bot").warning("V2_CAPTURE_FAILED error_class=%s", type(exc).__name__)


def active():
    return _observer.get() is not None


def publication_iso(value):
    """Normalize an explicit timezone only; never guess the machine timezone."""
    try:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def page_excerpt(html):
    """Capture visible source text and explicit dates; no candidate-date fallback."""
    from html.parser import HTMLParser
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.text, self.title, self.date = [], [], ""
            self.in_title, self.in_head, self.skip = False, False, 0
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "title": self.in_title = True
            if tag == "head": self.in_head = True
            if tag in {"script", "style"}: self.skip += 1
            if tag == "meta" and (attrs.get("property") or attrs.get("name") or "").lower() in {"article:published_time", "datepublished", "pubdate"}:
                self.date = attrs.get("content", "")
            if tag == "time" and not self.date: self.date = attrs.get("datetime", "")
        def handle_endtag(self, tag):
            if tag == "title": self.in_title = False
            if tag == "head": self.in_head = False
            if tag in {"script", "style"}: self.skip = max(0, self.skip - 1)
        def handle_data(self, data):
            if self.in_title: self.title.append(data)
            elif not self.in_head and not self.skip: self.text.append(data)
    parser = Parser()
    parser.feed(html[:2_000_000])
    return " ".join(" ".join(parser.title).split()), publication_iso(parser.date), parser.date, " ".join(" ".join(parser.text).split())[:12_000]
