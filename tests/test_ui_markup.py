"""The dashboard's scripts and page agree: every element a script looks up exists, and
the page's tags are balanced."""

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).parents[1] / "app/ui/static"
VOID = {"meta", "link", "img", "input", "br", "hr", "source", "path", "rect", "circle"}


def test_every_element_the_scripts_use_exists():
    for page_file in STATIC.glob("*.html"):
        page = page_file.read_text()
        ids = set(re.findall(r'id="([^"]+)"', page))
        for name in re.findall(r'<script src="/ui/([a-z]+\.js)"', page):
            wanted = set(re.findall(r'\$\("([a-z0-9-]+)"\)', (STATIC / name).read_text()))
            missing = sorted(wanted - ids)
            assert not missing, f"{name} on {page_file.name} uses missing elements: {missing}"


def test_pages_tags_are_balanced():
    class Check(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.problems = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not self.stack or self.stack[-1] != tag:
                self.problems.append((tag, self.getpos()))
            else:
                self.stack.pop()

    for page_file in STATIC.glob("*.html"):
        check = Check()
        check.feed(page_file.read_text())
        assert check.problems == [] and check.stack == [], page_file.name
