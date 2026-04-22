from __future__ import annotations

import asyncio
import json
from typing import Optional

from config import settings


async def call_llm(prompt: str, system: str = "") -> str:
    """
    Call Gemini 2.5 Flash. Falls back to Groq, then Ollama if keys/limits are hit.
    Returns the raw text response string.
    """
    if settings.gemini_api_key:
        try:
            return await _call_gemini(prompt, system)
        except Exception as exc:
            print(f"[LLM] Gemini failed ({exc}). Falling back to Groq/Ollama...")

    if settings.groq_api_key:
        try:
            return await _call_groq(prompt, system)
        except Exception as exc:
            print(f"[LLM] Groq failed ({exc}). Falling back to Ollama...")

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
    Answer a dynamic application form question using the resume as context.

    Returns a short, first-person answer suitable for typing into a form field.
    Hard-capped at 300 characters so human_fill() never hits a timeout from
    typing a multi-paragraph LLM response into a single-line text box.
    """
    prompt = f"""\
RESUME:
{resume_text[:4000]}

FORM FIELD LABEL:
{question}

Instructions:
- Write ONE concise sentence (max 20 words) answering this form field.
- Use only real information from the resume above.
- Do NOT start with "I" if a short phrase will do (e.g. "3 years" not "I have 3 years").
- Do NOT add any explanation, preamble, or punctuation beyond the answer itself.
- If the question asks to "choose one" from a list, reply with ONLY that option word-for-word.
- If you cannot find a relevant answer in the resume, reply with a single dash: -
"""
    raw = await call_llm(prompt)
    # Strip markdown formatting the LLM might add
    answer = raw.strip().strip("*_`").strip()
    # Hard cap — prevents ElementHandle.type() timeout
    return answer[:300]
