from __future__ import annotations

import asyncio
import json
from typing import Optional

from config import settings


async def call_llm(prompt: str, system: str = "") -> str:
    """
<<<<<<< HEAD
    Call Gemini 1.5 Pro. Falls back to Ollama if Gemini fails or key is missing.
=======
    Call Gemini 2.5 Flash. Falls back to Groq, then Ollama if keys/limits are hit.
>>>>>>> a135004 (Updated..)
    Returns the raw text response string.
    """
    if settings.gemini_api_key:
        try:
            return await _call_gemini(prompt, system)
        except Exception as exc:
<<<<<<< HEAD
            print(f"[LLM] Gemini failed ({exc}). Falling back to Ollama...")
=======
            print(f"[LLM] Gemini failed ({exc}). Falling back to Groq/Ollama...")

    if settings.groq_api_key:
        try:
            return await _call_groq(prompt, system)
        except Exception as exc:
            print(f"[LLM] Groq failed ({exc}). Falling back to Ollama...")
>>>>>>> a135004 (Updated..)

    return await _call_ollama(prompt, system)


async def _call_gemini(prompt: str, system: str = "") -> str:
<<<<<<< HEAD
    """Call Gemini 1.5 Pro via google-generativeai SDK."""
=======
    """Call Gemini 2.5 Flash via google-generativeai SDK."""
>>>>>>> a135004 (Updated..)
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(
<<<<<<< HEAD
        model_name="gemini-1.5-pro",
=======
        model_name="gemini-2.5-flash",
>>>>>>> a135004 (Updated..)
        system_instruction=system if system else None,
    )

    # Run in executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content(prompt),
    )
    return response.text


<<<<<<< HEAD
=======
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


>>>>>>> a135004 (Updated..)
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
    Returns a concise 1–3 sentence first-person answer.
    """
    prompt = f"""\
RESUME:
{resume_text[:5000]}

QUESTION FROM APPLICATION FORM:
{question}

Write a concise, factual, first-person answer (1–3 sentences) using only real \
experiences from the resume. Be specific with project names and numbers where available.
Do not invent any information not present in the resume.
"""
    return await call_llm(prompt)
