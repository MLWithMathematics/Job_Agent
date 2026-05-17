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


async def dynamic_qa(question: str, resume_text: str) -> str:
    """
    Answer a dynamic application form question using the resume AND the
    user's profile_qa.yaml as context.

    The profile_qa.yaml is the PRIMARY source of truth for factual answers
    (stipend, start date, personal details).  The resume provides background
    context for open-ended questions.

    Returns a short, first-person answer suitable for typing into a form field.
    Hard-capped at 300 characters so human_fill() never hits a timeout from
    typing a multi-paragraph LLM response into a single-line text box.
    """
    profile_block = _load_profile_qa()

    prompt = f"""\
APPLICANT PROFILE (use these facts as the PRIMARY source — answer exactly with these values when they match):
{profile_block if profile_block else "(no profile file found)"}

RESUME (use for background context only):
{resume_text[:3000]}

FORM FIELD LABEL:
{question}

Instructions:
- First check if the APPLICANT PROFILE has a matching answer for this question.
  If it does, reply with EXACTLY that value (e.g. "10000", "Immediately", "Yes").
- If the profile doesn't have a match, use the RESUME to compose a short answer.
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
    # Hard cap — prevents ElementHandle.type() timeout
    return answer[:300]
