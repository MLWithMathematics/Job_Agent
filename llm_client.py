from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from config import settings


# ── Profile QA loader (cached) ────────────────────────────────────────────────
_profile_qa_cache: Optional[str] = None


def _load_profile_qa() -> str:
    """
    Load resume/profile_qa.yaml and return it as a formatted text block.
    Cached after first load — the file is read once per process lifetime.
    """
    global _profile_qa_cache
    if _profile_qa_cache is not None:
        return _profile_qa_cache

    path = settings.profile_qa_path
    if not os.path.exists(path):
        _profile_qa_cache = ""
        return ""

    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Build a clean text block: "key: value" per line
        lines = []
        for key, value in data.items():
            if value and str(value).strip():
                # Convert snake_case keys to readable labels
                readable_key = key.replace("_", " ").title()
                lines.append(f"- {readable_key}: {value}")
        _profile_qa_cache = "\n".join(lines)
    except ImportError:
        # No PyYAML — fall back to raw text read
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            # Simple parser: extract "key: value" lines, skip comments
            lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip().strip('"').strip("'")
                    if val:
                        readable_key = key.strip().replace("_", " ").title()
                        lines.append(f"- {readable_key}: {val}")
            _profile_qa_cache = "\n".join(lines)
        except Exception:
            _profile_qa_cache = ""
    except Exception:
        _profile_qa_cache = ""

    return _profile_qa_cache


async def call_llm(prompt: str, system: str = "") -> str:
    """
    Call Gemini 2.5 Flash. Falls back to Groq, then Ollama if keys/limits are hit.
    Returns the raw text response string.
    """
    if settings.gemini_api_key:
        try:
            return await _call_gemini(prompt, system)
        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str or "quota" in exc_str.lower():
                print(f"[LLM] Gemini rate limit/quota exceeded. Falling back to Groq/Ollama...")
            else:
                print(f"[LLM] Gemini failed ({exc_str}). Falling back to Groq/Ollama...")

    if settings.groq_api_key:
        try:
            return await _call_groq(prompt, system)
        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str or "rate limit" in exc_str.lower() or "quota" in exc_str.lower():
                print(f"[LLM] Groq rate limit exceeded. Falling back to Ollama...")
            else:
                print(f"[LLM] Groq failed ({exc_str}). Falling back to Ollama...")

    return await _call_ollama(prompt, system)


async def _call_gemini(prompt: str, system: str = "") -> str:
    """Call Gemini 2.5 Flash via google-generativeai SDK."""
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=system if system else None,
    )

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content(prompt),
    )
    return response.text


async def _call_groq(prompt: str, system: str = "") -> str:
    """Call Groq API using the official client."""
    import groq

    client = groq.Groq(api_key=settings.groq_api_key)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(
            messages=messages,
            model=settings.groq_model,
        )
    )
    return response.choices[0].message.content


async def _call_ollama(prompt: str, system: str = "") -> str:
    """Call local Ollama model."""
    import ollama

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: ollama.chat(
            model=settings.ollama_model,
            messages=messages,
        ),
    )
    return response["message"]["content"]



# ── Question-type detection patterns ──────────────────────────────────────────

import re as _re

QUESTION_PATTERNS: list[tuple[str, str, str]] = [
    # (regex_pattern, field_type, specialized_prompt_suffix)
    # Order matters — first match wins.

    # Numeric experience questions
    (
        r"how many years?.*(experience|work)",
        "numeric",
        "Reply with ONLY a single integer number (e.g. '0', '2', '5'). "
        "Count academic projects and personal projects as 0 years of professional experience. "
        "If unsure, reply '0'.",
    ),
    (
        r"years? of (experience|work|proficiency)",
        "numeric",
        "Reply with ONLY a single integer number. "
        "If unsure, reply '0'.",
    ),
    (
        r"(how many|number of).*(projects?|internships?|certifications?)",
        "numeric",
        "Reply with ONLY a single integer number. Count relevant items from the resume.",
    ),

    # Salary / stipend / CTC
    (
        r"(salary|ctc|compensation|stipend|pay).*(expectation|expect|range|requirement)",
        "numeric",
        "Reply with ONLY a number (no currency symbol, no 'LPA', no 'per month'). "
        "Use the APPLICANT PROFILE value if available.",
    ),

    # Yes/No questions
    (
        r"(are you|do you|have you|will you|can you|would you|is your)",
        "yesno",
        "Reply with ONLY 'Yes' or 'No'. Nothing else.",
    ),

    # Choose-one from list
    (
        r"choose one|select one|pick one",
        "choice",
        "Reply with ONLY the exact text of the chosen option, word-for-word. Nothing else.",
    ),

    # Motivational / "why" questions
    (
        r"(why do you want|what (interests|motivates|excites)|why are you (interested|applying))",
        "motivational",
        "Write 1-2 concise sentences showing genuine interest in the SPECIFIC company and role. "
        "Reference a concrete detail from the job description. Stay under 40 words.",
    ),

    # Project / example questions
    (
        r"(describe a project|give an example|tell us about|share.*experience)",
        "project",
        "Describe ONE specific project from the resume relevant to this role. "
        "Use this format: 'Built [what] using [tech] — [result/impact]'. Stay under 40 words.",
    ),

    # URL fields
    (
        r"(url|link|portfolio|github|linkedin|website)",
        "url",
        "Reply with ONLY a valid URL starting with 'https://'. "
        "If no relevant URL exists in the profile, reply 'N/A'.",
    ),
]


def _detect_question_type(question: str) -> tuple[str, str]:
    """
    Match a form question against QUESTION_PATTERNS.
    Returns (field_type, specialized_prompt_suffix).
    Falls back to ("free_text", "") if no pattern matches.
    """
    q_lower = question.lower()
    for pattern, field_type, prompt_suffix in QUESTION_PATTERNS:
        if _re.search(pattern, q_lower):
            return field_type, prompt_suffix
    return "free_text", ""


async def dynamic_qa(question: str, resume_text: str, job_context: str = "") -> str:
    """
    Answer a dynamic application form question using the resume, the
    user's profile_qa.yaml, AND the job context (title + JD snippet).

    The profile_qa.yaml is the PRIMARY source of truth for factual answers
    (stipend, start date, personal details).  The resume provides background
    context for open-ended questions.  The job_context tells the LLM what
    role/company the user is applying for so answers are role-appropriate.

    Returns a short, first-person answer suitable for typing into a form field.
    Hard-capped at 300 characters so human_fill() never hits a timeout from
    typing a multi-paragraph LLM response into a single-line text box.
    """
    profile_block = _load_profile_qa()

    # Detect question type for specialized prompting
    field_type, type_prompt = _detect_question_type(question)

    # Build the job context block
    job_block = ""
    if job_context:
        job_block = f"""
JOB BEING APPLIED FOR (use this to tailor your answer):
{job_context[:1500]}
"""

    # Build the type-specific instruction
    type_instruction = ""
    if type_prompt:
        type_instruction = f"\n** CRITICAL FORMAT RULE: {type_prompt} **\n"

    prompt = f"""\
APPLICANT PROFILE (use these facts as the PRIMARY source — answer exactly with these values when they match):
{profile_block if profile_block else "(no profile file found)"}
{job_block}
RESUME (use for background context only):
{resume_text[:3000]}

FORM FIELD LABEL:
{question}
{type_instruction}
Instructions:
- First check if the APPLICANT PROFILE has a matching answer for this question.
  If it does, reply with EXACTLY that value (e.g. "10000", "Immediately", "Yes").
- If the profile doesn't have a match, use the RESUME and JOB CONTEXT to compose a short answer.
- Write ONE concise phrase or sentence (max 20 words).
- Do NOT start with "I" if a short phrase will do (e.g. "3 years" not "I have 3 years").
- Do NOT add any explanation, preamble, markdown, or punctuation beyond the answer itself.
- If the question asks to "choose one" from a list, reply with ONLY that option word-for-word.
- If the question asks to choose 'Yes' or 'No', reply with ONLY 'Yes' or 'No'.
- For numeric fields (salary, stipend, experience years), reply with ONLY the number.
- For date fields, reply with a short phrase like "Immediately" or "2026-06-01".
- If you cannot find a relevant answer in either source, make a highly plausible, safe guess based on context (e.g. "0" for expected salary, "Yes" for standard requirements, "None" for text). Never leave it blank or reply with a single dash.
"""
    try:
        raw = await call_llm(prompt)
    except Exception as exc:
        print(f"[LLM] Dynamic Q failed ({exc}). Leaving field blank.")
        return ""
    # Strip markdown formatting the LLM might add
    answer = raw.strip().strip("*_`").strip()

    # Post-process based on detected field type
    answer = _postprocess_answer(answer, field_type)

    # Hard cap — prevents ElementHandle.type() timeout
    return answer[:300]


def _postprocess_answer(answer: str, field_type: str) -> str:
    """
    Clean up the LLM answer based on detected field type.
    Ensures numeric fields get pure numbers, dates get proper format, etc.
    """
    if not answer:
        return answer

    if field_type == "numeric":
        # Extract the first number from the answer
        nums = _re.findall(r"\d+(?:\.\d+)?", answer)
        if nums:
            # Use the first number found
            return nums[0]
        # If no number found, check for word-numbers
        word_map = {
            "zero": "0", "one": "1", "two": "2", "three": "3",
            "four": "4", "five": "5", "six": "6", "seven": "7",
            "eight": "8", "nine": "9", "ten": "10",
        }
        for word, digit in word_map.items():
            if word in answer.lower():
                return digit
        return "0"  # safe default for numeric fields

    elif field_type == "yesno":
        lower = answer.lower().strip()
        if "yes" in lower:
            return "Yes"
        elif "no" in lower:
            return "No"
        return "Yes"  # safe default

    elif field_type == "url":
        answer = answer.strip()
        if answer.lower() in ("n/a", "na", "none", "nil", "-"):
            return "N/A"
        if answer and not answer.startswith(("http://", "https://")):
            answer = "https://" + answer
        return answer

    return answer

