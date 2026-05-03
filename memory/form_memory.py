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
    full_name = settings.full_name or " ".join(
        part for part in (settings.first_name, settings.last_name) if part
    ).strip()
    email = settings.email or settings.linkedin_email or settings.naukri_email

    defaults = {
        # Basic contact
        "phone": settings.phone,
        "mobile": settings.phone,
        "mobile number": settings.phone,
        "contact number": settings.phone,
        "phone number": settings.phone,
        "current location": settings.current_location,
        "location": settings.current_location,
        "city": settings.current_location,
        # Work terms
        "notice period": settings.notice_period,
        "current ctc": settings.current_ctc,
        "current salary": settings.current_ctc,
        "expected ctc": settings.expected_ctc,
        "expected salary": settings.expected_ctc,
        "total experience years": settings.total_experience_years,
        "years of experience": settings.total_experience_years,
        "experience": settings.total_experience_years,
        "total experience": settings.total_experience_years,
        # Identity
        "full name": full_name,
        "name": full_name,
        "first name": settings.first_name,
        "last name": settings.last_name,
        "surname": settings.last_name,
        "email": email,
        "email address": email,
        "email id": email,
        # Online profiles
        "linkedin": settings.linkedin_url,
        "linkedin url": settings.linkedin_url,
        "linkedin profile": settings.linkedin_url,
        "linkedin profile url": settings.linkedin_url,
        "github": settings.github_url,
        "github url": settings.github_url,
        "github profile": settings.github_url,
        "portfolio": settings.portfolio_url,
        "portfolio url": settings.portfolio_url,
        "website": settings.portfolio_url,
        "personal website": settings.portfolio_url,
        # Education
        "college": settings.college,
        "university": settings.college,
        "institution": settings.college,
        "school": settings.college,
        "degree": settings.degree,
        "qualification": settings.degree,
        "highest qualification": settings.degree,
        "graduation year": settings.graduation_year,
        "year of graduation": settings.graduation_year,
        "passing year": settings.graduation_year,
        # Current position
        "current company": settings.current_company,
        "current employer": settings.current_company,
        "company name": settings.current_company,
        "current role": settings.current_role,
        "current job title": settings.current_role,
        "job title": settings.current_role,
        "designation": settings.current_role,
        # Eligibility — these are commonly asked as yes/no
        "are you authorized to work": settings.work_authorization,
        "work authorization": settings.work_authorization,
        "are you legally eligible": settings.work_authorization,
        "eligible to work in india": settings.work_authorization,
        "eligible to work": settings.work_authorization,
        "require visa sponsorship": "No",
        "do you require sponsorship": "No",
        "visa sponsorship": "No",
        "gender": settings.gender,
        "nationality": settings.nationality,
        # Common yes/no questions answered by default
        "are you a fresher": "No",
        "fresher": "No",
        "are you currently employed": "Yes" if settings.current_company else "No",
        "currently employed": "Yes" if settings.current_company else "No",
        "willing to relocate": "Yes",
        "open to relocation": "Yes",
        "immediate joiner": "Yes" if settings.notice_period in ("0", "immediate", "Immediate") else "No",
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
