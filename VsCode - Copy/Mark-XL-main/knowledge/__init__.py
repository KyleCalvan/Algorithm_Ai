"""Course lesson knowledge base — .docx module indexing and retrieval."""
from .lesson_manager import (
    build_index,
    warmup,
    query,
    list_lessons,
    get_lesson,
    lesson_full_text,
    format_sections,
    format_lessons_for_prompt,
    export_lesson,
    resolve_lesson,
    stats,
    get_lessons_dir,
    lesson_action,
)

__all__ = [
    "build_index", "warmup", "query", "list_lessons", "get_lesson",
    "lesson_full_text", "format_sections", "format_lessons_for_prompt",
    "export_lesson", "resolve_lesson", "stats", "get_lessons_dir", "lesson_action",
]
