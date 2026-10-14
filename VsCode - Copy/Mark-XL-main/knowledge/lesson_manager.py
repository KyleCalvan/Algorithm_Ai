"""
Lesson knowledge base for MARK XL.

Turns the .docx course modules in  knowledge/MEMORY_MODULES/  into a searchable
index that Jarvis can answer from.  Uses ONLY the Python standard library —
a .docx is just a zip of XML, so no python-docx dependency is required.

Public API
──────────
    build_index(force=False)           → rebuild (or load cached) index
    query(question, top_k=4, lesson=…) → ranked lesson sections for a question
    list_lessons()                     → [{id, title, sections, headings, …}]
    get_lesson(lesson_id)              → one module record (with full text)
    export_lesson(lesson_id, out_dir)  → write a .md copy; returns path
    format_lessons_for_prompt()        → compact outline for the system prompt
    resolve_lesson(name)               → "group 5" / "module 3" → lesson id
    stats()                            → corpus statistics
"""
import html as _html
import json
import math
import re
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path

# The desktop console is cp1252 by default on Windows; never let an emoji in a
# log line crash the whole assistant.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
KNOWLEDGE_DIR    = BASE_DIR / "knowledge"
DEFAULT_LESSONS  = KNOWLEDGE_DIR / "MEMORY_MODULES"
CACHE_PATH       = KNOWLEDGE_DIR / "cache" / "lessons.json"

_lock: threading.Lock = threading.Lock()
_index: dict | None = None

# ── .docx extraction (stdlib only) ────────────────────────────────────────────

_TAG = re.compile(r"<[^>]+>")
_T   = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)

# Word allows a <w:tbl> to nest inside a <w:tc>, so blocks / rows / cells have to
# be split by walking the tags with a depth counter.  A non-greedy regex stops at
# the *inner* closing tag: it truncated the outer table at the nested one and
# leaked the outer table's remaining rows out as stray paragraphs.
_WTAG = re.compile(r"<(/?)w:(p|tbl|tr|tc)\b[^>]*?(/?)>")


def _inner(chunk: str) -> str:
    """The content between a matched element's own opening and closing tags."""
    return chunk[chunk.index(">") + 1 : chunk.rindex("</w:")]


def _top_blocks(xml: str) -> list[tuple[str, str]]:
    """Ordered top-level (kind, xml) blocks, honouring nested tables."""
    out: list[tuple[str, str]] = []
    stack: list[tuple[str, int]] = []          # [(kind, start-of-opening-tag)]
    for m in _WTAG.finditer(xml):
        if m.group(3):                          # self-closing tag — nothing to balance
            continue
        name = m.group(2)
        if m.group(1):                         # closing tag
            if stack and stack[-1][0] == name:
                start = stack.pop()[1]
                if not stack:                  # back at top level → one whole block
                    out.append((name, xml[start : m.end()]))
        elif not stack or name in ("tbl", "tr", "tc"):
            stack.append((name, m.start()))
    return out


def _spans(xml: str, tag: str) -> list[str]:
    """Top-level <w:{tag}>…</w:{tag}> chunks of *xml*, honouring nested tables."""
    return [chunk for kind, chunk in _top_blocks(xml) if kind == tag]


def _plain(chunk: str) -> str:
    """Concatenate the <w:t> runs of one block, unescape entities, strip strays."""
    joined = "".join(_T.findall(chunk))
    # Strip leftover markup BEFORE unescaping, and never the other way round:
    # unescaping first turns "&lt;b&gt;" into a real "<b>", which the tag
    # stripper then swallows whole, deleting the text between them.
    return _html.unescape(_TAG.sub("", joined)).strip()


def extract_docx(path: Path) -> tuple[list[dict], int]:
    """
    Parse a Word document into an ordered block list.

    Returns (blocks, image_count) where each block is either
        {"kind": "p",     "style": str, "text": str}
        {"kind": "table", "rows": [[cell, …], …]}
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        xml = z.read("word/document.xml").decode("utf-8", "replace")

    images = sum(1 for n in names if n.startswith("word/media/"))
    body_m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
    body = body_m.group(1) if body_m else xml

    blocks: list[dict] = []
    for kind, chunk in _top_blocks(body):
        if kind == "p":
            style_m = re.search(r'<w:pStyle w:val="([^"]+)"', chunk)
            text = _plain(chunk)
            if text:
                blocks.append({
                    "kind":  "p",
                    "style": (style_m.group(1) if style_m else ""),
                    "text":  text,
                    "bold":  _whole_para_bold(chunk),
                })
        elif kind == "tbl":
            rows = _table_rows(chunk)
            if rows:
                blocks.append({"kind": "table", "rows": rows})
    return blocks, images


def _table_rows(tbl: str) -> list[list[str]]:
    """Rows of one <w:tbl>, as a list of cell-text lists."""
    rows = []
    for row in _spans(_inner(tbl), "tr"):
        cells = [_cell_text(c) for c in _spans(_inner(row), "tc")]
        if any(cells):
            rows.append(cells)
    return rows


def _cell_text(cell: str) -> str:
    """Text of one cell; nested tables become markdown instead of leaking out."""
    body = _inner(cell)                     # drop the cell's own <w:tc> wrapper
    paras = [p for p in (_plain(x) for x in _spans(body, "p")) if p]
    nested = [_table_to_markdown(_table_rows(t)) for t in _spans(body, "tbl")]
    text = "\n".join(paras)
    if nested:
        text = (text + "\n\n" + "\n\n".join(nested)).strip()
    return text


def _heading_level(style: str) -> int | None:
    """Title → 0, Heading1 → 1, Heading2 → 2, …  Anything else → None."""
    s = (style or "").strip().lower()
    m = re.match(r"heading\s*(\d+)$", s)
    if m:
        return int(m.group(1))
    if s == "title":
        return 0
    if s == "subtitle":
        return 1
    return None


_B_FALSE = re.compile(r'w:val\s*=\s*"(0|false|off)"', re.I)


def _whole_para_bold(chunk: str) -> bool:
    """True only when the paragraph's *visible* text is bold throughout.

    Two real-.docx wrinkles make this more than a tag search:

    *  Word routinely appends a formatting-only run — ``<w:r><w:rPr><w:rtl
       w:val="0"/></w:rPr></w:r>`` with no ``<w:t>`` — after every heading.
       Ignoring runs that carry no text keeps the heading bold; requiring every
       run to carry a ``<w:b>`` reads those empty runs as non-bold and loses
       every heading in a document formatted this way.
    *  Bold can also be declared on the paragraph mark
       (``<w:pPr><w:rPr><w:b/></w:rPr></w:pPr>``), in which case a run with no
       formatting of its own inherits it.

    And a paragraph with one emphasised word is not a heading, so a text run
    that is not bold (or explicitly un-bolds with ``w:val="0"``) disqualifies it.
    """
    ppr = re.search(r"<w:pPr\b.*?</w:pPr>|<w:pPr\b[^>]*/>", chunk, re.S)
    ppr_bold = False
    if ppr:
        b = re.search(r"<w:b\b[^>]*>", ppr.group(0))
        ppr_bold = bool(b) and not _B_FALSE.search(b.group(0))

    runs = re.findall(r"<w:r\b.*?</w:r>|<w:r\b[^>]*/>", chunk, re.S)
    if not runs:
        return False
    any_text = False
    for run in runs:
        if not re.search(r"<w:t\b[^>]*>|<w:tab\b[^>]*>|<w:br\b[^>]*>", run):
            continue                            # nothing visible → cannot un-bold the line
        any_text = True
        b = re.search(r"<w:b\b[^>]*>", run)
        if b and _B_FALSE.search(b.group(0)):
            return False                        # explicit bold-off on visible text
        if not b and not ppr_bold:
            return False                        # no bold, and none inherited
    return any_text


def _looks_like_heading(text: str) -> bool:
    """Fallback heuristic: short, bold, non-sentence paragraphs act as headings."""
    if not text or len(text) > 90 or len(text.split()) > 12:
        return False
    return not re.search(r"[.!?]$", text)


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    out = []
    for i, row in enumerate(rows):
        # A literal '|' in a cell would add a phantom column, so escape it the
        # way GitHub-Flavoured Markdown expects.  The separator width is still
        # taken from the *unescaped* cell so the dashes line up.
        cells = [c.replace("\n", " ").strip().replace("|", r"\|") for c in row]
        out.append(" | ".join(cells))
        if i == 0:
            out.append(" | ".join("-" * max(3, len(c)) for c in row))
    return "\n".join(out)


def build_sections(blocks: list[dict], lesson_id: str, lesson_title: str) -> list[dict]:
    """Group ordered blocks into sections, split on heading paragraphs."""
    sections: list[dict] = []

    # Only fall back to bold-paragraph headings when the document defines no
    # Heading styles at all.  In a normally-styled document a bold body line
    # ("**Note:** ...") is not a heading, and promoting it would split the
    # section and drop the sentence out of the body text.
    has_styles = any(b["kind"] == "p" and _heading_level(b["style"]) is not None
                     for b in blocks)

    path: list[str] = []
    cur = {"heading": "", "path": [], "parts": [], "index": 0}

    def flush():
        text = "\n".join(cur["parts"]).strip()
        if text:
            sections.append({
                "lesson_id":    lesson_id,
                "lesson_title": lesson_title,
                "heading":      cur["heading"],
                "path":         list(cur["path"]),
                "index":        cur["index"],
                "text":         text,
            })

    for b in blocks:
        if b["kind"] == "table":
            md = _table_to_markdown(b["rows"])
            if md:
                cur["parts"].append(md)
            continue

        lvl = _heading_level(b["style"])
        if lvl is None and not has_styles and b.get("bold") and _looks_like_heading(b["text"]):
            lvl = 1          # no Heading styles in this doc — use bold lines instead
        if lvl is None:
            cur["parts"].append(b["text"])
            continue

        flush()
        if lvl >= 1:
            if _heading_level(b["style"]) is None:
                # A fallback heading: the document is flat, so this line is a
                # sibling of the previous one.  Keep the module title as its
                # ancestry rather than resetting the path outright, which would
                # orphan every section from its own document title.
                path = path[:1] + [b["text"]]
            else:
                path = path[: lvl - 1] + [b["text"]]
        else:
            path = []
        cur = {
            "heading": b["text"],
            "path":    list(path),
            "parts":   [],
            "index":   len(sections),
        }

    flush()
    return sections


# ── index build / cache ───────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_lessons_dir() -> Path:
    """
    Where the .docx modules live, in priority order:
      1. "lessons_path" in config/api_keys.json   (absolute override)
      2. <project>/knowledge/MEMORY_MODULES       (bundled copy — default)
      3. <project>/../MEMORY_MODULES
      4. <project>/../../MEMORY_MODULES            (the repo-root layout)
    """
    cfg = _load_config()
    configured = (cfg.get("lessons_path") or "").strip()
    if configured:
        p = Path(configured)
        if p.is_dir() and any(p.glob("*.docx")):
            return p

    candidates = [
        DEFAULT_LESSONS,
        BASE_DIR.parent / "MEMORY_MODULES",
        BASE_DIR.parent.parent / "MEMORY_MODULES",
    ]
    for p in candidates:
        if p.is_dir() and any(p.glob("*.docx")):
            return p
    return DEFAULT_LESSONS


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", Path(name).stem.lower()).strip("_")
    return s or "lesson"


def _pretty_title(stem: str) -> str:
    """'Module6_Advanced_Algorithms' → 'Module 6 Advanced Algorithms'."""
    s = stem.replace("_", " ")
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _aliases_for(stem: str, title: str) -> list[str]:
    aliases = {stem.lower(), title.lower()}
    for pattern in (r"group\s*(\d+)", r"module\s*(\d+)"):
        m = re.search(pattern, title.lower())
        if m:
            num = m.group(1)
            aliases.add(f"group {num}")
            aliases.add(f"module {num}")
            aliases.add(num)
    # The title minus its "GROUP 5 - " / "Module 6 " prefix — the part that
    # actually names the subject.  Without it "graph algorithms" never resolves,
    # because the stored title is the singular "GROUP 5 - GRAPH ALGORITHM" and
    # neither string contains the other.
    subject = re.sub(r"^(group|module|unit|chapter|part|week)\s*\d+\s*[-–:]\s*",
                     "", title.lower()).strip()
    if subject and subject != title.lower():
        aliases.add(subject)
        aliases.add(subject.rstrip("s"))          # singular, for plural queries
        aliases.add(subject + "s")                # plural, for singular titles
    return sorted(aliases)


def _load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "sections" in data else None
    except Exception as e:
        print(f"[Lessons] ⚠️ Cache unreadable ({e}); rebuilding.")
        return None


def _save_cache(index: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[Lessons] ⚠️ Could not write cache: {e}")


def build_index(force: bool = False) -> dict:
    """
    Parse every .docx module into lessons + sections.  Cached to disk and
    keyed on file mtimes, so it only re-parses when a document changes.
    """
    global _index
    with _lock:
        # Fingerprints are needed by both the in-memory check below and the
        # on-disk cache check, so compute them once, before any short-circuit.
        lessons_dir = get_lessons_dir()
        files = sorted(lessons_dir.glob("*.docx"))
        try:
            fingerprints = {f.name: f.stat().st_mtime for f in files}
        except OSError:
            fingerprints = {}          # a module vanished mid-scan → rebuild

        # A warm in-memory index short-circuits the parse, but the mtimes still
        # have to be compared, or a module added, edited or replaced after the
        # first build stays invisible for the whole life of the process.
        if _index is not None and not force:
            if fingerprints == _index.get("files"):
                return _index
            force = True               # the corpus changed on disk → re-parse

        if not files:
            _index = {"version": 1, "built_at": None, "lessons": [],
                      "sections": [], "files": {}, "empty": True}
            return _index

        if not force:
            cached = _load_cache()
            if cached and cached.get("files") == fingerprints and cached.get("version") == 1:
                _index = cached
                return _index

        lessons: list[dict] = []
        sections: list[dict] = []
        for f in files:
            try:
                blocks, images = extract_docx(f)
            except Exception as e:
                print(f"[Lessons] ⚠️ Failed to read {f.name}: {e}")
                continue

            stem  = f.stem
            lid   = _slugify(stem)
            title = _pretty_title(stem)
            secs  = build_sections(blocks, lid, title)

            lessons.append({
                "id":         lid,
                "title":      title,
                "file":       f.name,
                "path":       str(f),
                "chars":      sum(len(s["text"]) for s in secs),
                "paragraphs": sum(1 for b in blocks if b["kind"] == "p"),
                "tables":     sum(1 for b in blocks if b["kind"] == "table"),
                "images":     images,
                "sections":   len(secs),
                "headings":   [s["heading"] for s in secs if s["heading"]],
                "aliases":    _aliases_for(stem, title),
            })
            sections.extend(secs)

        _index = {
            "version":   1,
            "built_at":  datetime.now().isoformat(timespec="seconds"),
            "lessons":   lessons,
            "sections":  sections,
            "files":     fingerprints,
        }
        _save_cache(_index)
        print(
            f"[Lessons] 📚 Indexed {len(lessons)} modules · {len(sections)} sections · "
            f"{sum(l['chars'] for l in lessons)} chars"
        )
    return _index


def warmup() -> None:
    """Build the index off the main thread so startup stays responsive."""
    threading.Thread(target=build_index, kwargs={"force": False}, daemon=True).start()


# ── retrieval ─────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "so", "as", "at",
    "be", "been", "is", "are", "was", "were", "do", "does", "did", "of", "in",
    "to", "for", "on", "with", "by", "from", "into", "over", "under", "about",
    "it", "its", "this", "that", "these", "those", "there", "here", "all", "any",
    "each", "both", "few", "more", "most", "other", "some", "such", "not", "no",
    "what", "how", "why", "when", "which", "who", "whom", "whose", "can", "could",
    "you", "your", "my", "me", "we", "us", "our", "they", "them", "their", "he",
    "him", "his", "she", "her", "i", "also", "just", "very", "too", "up", "down",
    "out", "off", "new", "use", "used", "using", "one", "two", "will", "would",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS]


# Category filler that names no module in particular.  The fuzzy resolver must not
# let one of these latch onto whichever title happens to contain it: on the shipped
# corpus 'module' resolved to Module 6 and 'group' / 'algorithm' to Group 2 purely
# because the word was a substring of those titles.
_GENERIC_LESSON_WORDS = {
    "module", "modules", "group", "groups", "lesson", "lessons", "unit", "units",
    "chapter", "chapters", "section", "sections", "topic", "topics", "part",
    "parts", "week", "weeks", "day", "days", "lecture", "lectures", "tutorial",
    "tutorials", "lab", "labs", "course", "courses", "class", "classes",
    "algorithm", "algorithms", "recap", "revision", "notes", "note", "study",
    "material", "materials", "handout", "handouts", "reading", "readings",
    "assignment", "assignments", "homework", "subject", "subjects",
}


def _is_generic_key(key: str) -> bool:
    """True when *key* is only bare category filler ('module', 'group', 'lesson').

    Such a word names no module in particular, so the fuzzy passes must not let it
    latch onto whichever title happens to contain it — 'module' resolved to Module 6
    purely because 'module' is a substring of that title.  A key that carries a digit
    ('module6', 'group 5') points at one specific module, so it is never generic.
    """
    if any(c.isdigit() for c in key):
        return False
    return not any(t not in _GENERIC_LESSON_WORDS for t in re.findall(r"[a-z]+", key))


def resolve_lesson(name: str | None) -> str | None:
    """Map a loose name ('group 5', 'module 3', 'recurrence') onto a lesson id."""
    if not name:
        return None
    index = build_index()
    key = re.sub(r"\s+", " ", str(name).strip().lower())
    if not key:
        return None
    for lesson in index["lessons"]:
        if lesson["id"] == key or key in lesson["aliases"]:
            return lesson["id"]
    if _is_generic_key(key):
        return None          # 'module' / 'group' alone is ambiguous — ask instead of guessing
    for lesson in index["lessons"]:
        title = lesson["title"].lower()
        # `title in key` only counts when the title is actually what the key is
        # about — a one-word title ('Programming') must not be claimed by every
        # query that happens to mention the word.
        if key in title or (title in key and len(title) * 2 >= len(key)):
            return lesson["id"]
    for lesson in index["lessons"]:
        for alias in lesson["aliases"]:
            if alias and (key in alias or (alias in key and len(alias) * 2 >= len(key))):
                return lesson["id"]
    return None


def query(question: str, top_k: int = 4, lesson: str | None = None) -> list[dict]:
    """
    Keyword + IDF ranking of sections against a natural-language question.
    Heading matches are weighted 3× body matches.
    """
    index = build_index()
    sections = index.get("sections", [])
    if not sections:
        return []

    terms = _tokenize(question or "")
    if not terms:
        return []

    lid = resolve_lesson(lesson) if lesson else None

    n = len(sections)
    df: dict[str, int] = {}
    for s in sections:
        blob = (s["heading"] + " " + s["text"]).lower()
        for t in set(_tokenize(blob)):
            df[t] = df.get(t, 0) + 1

    scored: list[tuple[float, dict]] = []
    for s in sections:
        if lid and s["lesson_id"] != lid:
            continue
        heading_tokens = set(_tokenize(s["heading"]))
        body_counts: dict[str, int] = {}
        for t in _tokenize(s["text"]):
            body_counts[t] = body_counts.get(t, 0) + 1

        score = 0.0
        for t in terms:
            d = df.get(t)
            if not d:
                continue
            idf = math.log(1 + n / d)
            score += idf * ((3 if t in heading_tokens else 0) + body_counts.get(t, 0))
        if score <= 0:
            continue

        # Mild length normalisation so giant sections don't dominate.
        score /= 1 + len(s["text"]) / 1500.0
        scored.append((score, s))

    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [s for _, s in scored[: max(1, min(int(top_k or 4), 20))]]


# ── formatting / output ───────────────────────────────────────────────────────

def _breadcrumb(s: dict) -> str:
    return " › ".join([s["lesson_title"]] + [p for p in s["path"] if p])


def format_sections(sections: list[dict]) -> str:
    out = []
    for s in sections:
        out.append(f"──── {_breadcrumb(s)} ────\n{s['text']}")
    return "\n\n".join(out)


def list_lessons() -> list[dict]:
    return build_index().get("lessons", [])


def get_lesson(lesson_id: str) -> dict | None:
    for lesson in list_lessons():
        if lesson["id"] == lesson_id:
            return lesson
    return None


def lesson_full_text(lesson: dict) -> str:
    """All sections of one module, joined with their heading breadcrumbs."""
    index = build_index()
    out = []
    for s in index.get("sections", []):
        if s["lesson_id"] == lesson["id"]:
            crumb = " › ".join(p for p in s["path"] if p)
            header = f"## {crumb}" if crumb else f"## {lesson['title']}"
            out.append(f"{header}\n\n{s['text']}")
    return ("\n".join(out)).strip()


def format_lessons_for_prompt(max_chars: int = 1500) -> str:
    """Compact outline injected into the system prompt so Jarvis knows what it knows.

    The module titles always come out whole — they are what the model needs to
    decide the `lessons` tool is relevant.  Sub-headings are only added while
    they still fit, so an extra module can never push the outline past its cap
    and get itself cut off mid-word.
    """
    lessons = list_lessons()
    if not lessons:
        return ""

    intro = ("[YOUR COURSE KNOWLEDGE BASE — lesson modules you can answer from. "
             "When asked about these topics, call the 'lessons' tool to retrieve the "
             "exact source text, then teach from it.]")
    modules = [f"• {l['title']} — {l['sections']} sections" for l in lessons]

    lines = [intro, *modules]
    if sum(len(x) + 1 for x in lines) > max_chars:
        # Even the module list is too long: keep as many whole modules as fit.
        out, total = [intro], len(intro)
        for line in modules:
            if total + len(line) + 3 > max_chars:
                break
            out.append(line)
            total += len(line) + 1
        return "\n".join(out) + "\n…(more modules — call the 'lessons' tool)\n"

    for lesson in lessons:
        for h in lesson["headings"][:8]:
            lines.append(f"    {h[:70]}")
            if sum(len(x) + 1 for x in lines) > max_chars:
                # Drop the heading that overflowed and stop.
                lines.pop()
                return "\n".join(lines) + "\n…(more topics inside each module)\n"
    return "\n".join(lines) + "\n"


def export_lesson(lesson_id: str, out_dir: str | None = None) -> str:
    """Write one module to a Markdown file. Returns the path, or an error string."""
    lesson = get_lesson(lesson_id)
    if not lesson:
        return f"Lesson not found: {lesson_id}"

    base = Path(out_dir) if out_dir else (BASE_DIR / "output" / "lessons")
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"{_slugify(lesson['title'])}.md"

    body = lesson_full_text(lesson)
    header = (
        f"# {lesson['title']}\n\n"
        f"*{lesson['paragraphs']} paragraphs · {lesson['tables']} tables · "
        f"{lesson['images']} images · {lesson['chars']} characters*\n\n"
        f"*Source: `{lesson['file']}`*\n\n---\n\n"
    )
    out.write_text(header + body + "\n", encoding="utf-8")
    return str(out)


def stats() -> dict:
    index = build_index()
    return {
        "modules":     len(index.get("lessons", [])),
        "sections":    len(index.get("sections", [])),
        "characters":  sum(l.get("chars", 0) for l in index.get("lessons", [])),
        "tables":      sum(l.get("tables", 0) for l in index.get("lessons", [])),
        "images":      sum(l.get("images", 0) for l in index.get("lessons", [])),
        "built_at":    index.get("built_at"),
        "lessons_dir": str(get_lessons_dir()),
    }


# ── tool dispatch (called from main.py) ───────────────────────────────────────

_READ_CHAR_CAP = 12000


def lesson_action(parameters: dict | None = None, player=None) -> str:
    """Handle the 'lessons' tool call and return text the assistant can speak from."""
    args = parameters or {}
    action = (args.get("action") or "search").strip().lower()

    def _log(msg: str):
        if player:
            try:
                player.write_log(msg)
            except Exception:
                pass

    if action in ("list", "outline", "modules"):
        lessons = list_lessons()
        if not lessons:
            return ("The lesson knowledge base is empty, sir — no .docx modules were found in "
                    f"{get_lessons_dir()}.")
        lines = [f"You have {len(lessons)} course modules, sir:"]
        for lesson in lessons:
            lines.append(f"• {lesson['title']} — {lesson['sections']} sections, "
                         f"{lesson['chars']} characters")
            for h in lesson["headings"][:6]:
                lines.append(f"    {h}")
        return "\n".join(lines)

    if action in ("stats", "info"):
        s = stats()
        return (f"Knowledge base: {s['modules']} modules, {s['sections']} sections, "
                f"{s['characters']} characters, {s['tables']} tables, {s['images']} images. "
                f"Source folder: {s['lessons_dir']}.")

    if action in ("read", "full", "module"):
        lid = resolve_lesson(args.get("lesson")) or resolve_lesson(args.get("query"))
        lesson = get_lesson(lid) if lid else None
        if not lesson:
            return ("I couldn't find that module, sir. Say 'list lessons' to see which "
                    "modules you have, or give me a title like 'graph algorithms' or 'module 5'.")
        _log(f"[lessons] read: {lesson['title']}")
        text = lesson_full_text(lesson)
        if len(text) > _READ_CHAR_CAP:
            text = text[:_READ_CHAR_CAP].rstrip() + f"\n\n…(truncated — {lesson['chars']} chars total)"
        return f"# {lesson['title']}\n\n{text}"

    if action == "export":
        lid = resolve_lesson(args.get("lesson")) or resolve_lesson(args.get("query"))
        if not lid:
            return ("Tell me which module to export, sir — for example 'export module 3' "
                    "or 'export the recurrence relations lesson'.")
        try:
            out = export_lesson(lid)
        except Exception as e:
            return (f"I found the module but couldn't write the file, sir — {e}. "
                    "The output folder may be read-only or in use.")
        if out.startswith("Lesson not found"):
            return f"I couldn't find that module, sir — {out}"
        _log(f"[lessons] exported: {out}")
        return f"Exported the module to a Markdown file, sir: {out}"

    # default: search
    q_text = (args.get("query") or args.get("question") or args.get("topic") or "").strip()
    if not q_text:
        return ("Give me a question or topic to look up in your lesson modules, sir — "
                "for example 'search for the master theorem'.")
    try:
        top_k = int(args.get("top_k") or 4)
    except (TypeError, ValueError):
        top_k = 4
    top_k = max(1, min(top_k, 8))

    lesson_filter = args.get("lesson")
    sections = query(q_text, top_k=top_k, lesson=lesson_filter)
    if not sections:
        return (f"Nothing in your lesson modules matches '{q_text}', sir. Try different "
                "keywords, or ask me to list the modules so we can see what's covered.")
    _log(f"[lessons] search '{q_text}' → {len(sections)} section(s)")
    body = format_sections(sections)
    # A filter that didn't resolve to any module is ignored by the search, not
    # fatal — but silently searching everything is confusing when the user asked
    # for one specific module, so say so.
    if lesson_filter and not resolve_lesson(lesson_filter):
        return (f"I couldn't find a module called '{lesson_filter}', sir, so I searched "
                f"all of them instead.\n\nFrom your lesson modules "
                f"({len(sections)} matching sections for '{q_text}'):\n\n" + body)
    return (f"From your lesson modules ({len(sections)} matching sections for '{q_text}'):\n\n"
            + body)

