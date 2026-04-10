from __future__ import annotations

import json
import os
import re
import difflib
from typing import Optional, Dict

from config import settings


def _normalize(label: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    label = label.lower()
    label = re.sub(r"[^\w\s]", "", label)
    label = re.sub(r"\s+", " ", label).strip()
    return label


def _load() -> Dict[str, str]:
    if not os.path.exists(settings.form_memory_path):
        return {}
    with open(settings.form_memory_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(settings.form_memory_path), exist_ok=True)
    with open(settings.form_memory_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _seed_defaults() -> None:
    """Pre-populate from .env values if memory file is empty / missing."""
    data = _load()
    defaults = {
        "phone": settings.phone,
        "current location": settings.current_location,
        "notice period": settings.notice_period,
        "current ctc": settings.current_ctc,
        "expected ctc": settings.expected_ctc,
        "total experience years": settings.total_experience_years,
        "years of experience": settings.total_experience_years,
    }
    updated = False
    for key, val in defaults.items():
        if val and key not in data:
            data[key] = val
            updated = True
    if updated:
        _save(data)


def get_answer(label: str) -> Optional[str]:
    """
    Return saved answer for this label, using fuzzy matching.
    Returns None if no match found.
    """
    data = _load()
    norm = _normalize(label)

    # Exact match first
    if norm in data:
        return data[norm]

    # Fuzzy match
    keys = list(data.keys())
    matches = difflib.get_close_matches(norm, keys, n=1, cutoff=0.85)
    if matches:
        return data[matches[0]]

    return None


def save_answer(label: str, value: str) -> None:
    """Persist a new field answer."""
    data = _load()
    norm = _normalize(label)
    data[norm] = value
    _save(data)


def list_all() -> Dict[str, str]:
    return _load()


# Seed defaults on import
_seed_defaults()
