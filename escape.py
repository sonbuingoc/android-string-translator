import re

HTML_TAG_PATTERN = re.compile(r"</?[a-zA-Z]+[^>]*>")

def android_escape(text: str) -> str:
    if text is None:
        return ""

    original = text

    # 0) Bảo vệ các dấu nháy đơn đã escape sẵn: \'
    protected_quotes = {}
    def protect_escaped_quote(match):
        key = f"__ESCAPED_QUOTE_{len(protected_quotes)}__"
        protected_quotes[key] = match.group(0)
        return key

    safe = re.sub(r"\\'", protect_escaped_quote, original)

    # 1) Bảo vệ HTML tag
    html_tags = {}
    def protect_html(match):
        key = f"__HTML_TAG_{len(html_tags)}__"
        html_tags[key] = match.group(0)
        return key

    safe = HTML_TAG_PATTERN.sub(protect_html, safe)

    # 2) Escape XML bắt buộc
    safe = safe.replace("&", "&amp;")
    safe = safe.replace("<", "&lt;")
    safe = safe.replace(">", "&gt;")

    # 3) Escape dấu nháy đơn còn lại
    safe = safe.replace("'", "\\'")

    # 4) Khôi phục HTML tag
    for key, tag in html_tags.items():
        safe = safe.replace(key, tag)

    # 5) Khôi phục các \'
    for key, value in protected_quotes.items():
        safe = safe.replace(key, value)

    return safe

