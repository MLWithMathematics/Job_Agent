from __future__ import annotations

import json
import re
from typing import List

from agents.search_agent import JobListing
from agents.scorer_agent import ScorerOutput
from resume.resume_editor import edit_resume
from llm_client import call_llm
from config import settings


TAILORING_PROMPT = """\
<<<<<<< HEAD
You are a professional resume writer.
=======
You are an expert resume writer focused on ATS (Applicant Tracking System) optimization.
>>>>>>> a135004 (Updated..)

ORIGINAL RESUME BULLETS:
{bullet_list}

<<<<<<< HEAD
SKILLS TO HIGHLIGHT:
=======
SKILLS TO HIGHLIGHT (from job description):
>>>>>>> a135004 (Updated..)
{matched_skills}

JD KEYWORDS TO WEAVE IN:
{top_keywords}

<<<<<<< HEAD
Rewrite each bullet point to subtly emphasize the above skills and naturally \
incorporate the JD keywords. Keep all facts 100% accurate. \
Do NOT invent metrics, projects, or experiences that are not in the originals.
=======
Rewrite each bullet point so that your output embodies a fully ATS-compliant style:
- Use clear action verbs (e.g., Developed, Designed, Spearheaded).
- Quantify achievements where possible without inventing new facts.
- Seamlessly weave in the provided skills and keywords so it perfectly matches the job description.
- Keep sentences structure simple and linear for ATS parsers.
>>>>>>> a135004 (Updated..)

Return ONLY a JSON array of rewritten bullet strings, exactly the same count \
as the input bullets. No markdown, no preamble, no explanation.
Example: ["Rewritten bullet 1.", "Rewritten bullet 2.", ...]
"""


async def run_tailoring_agent(
    job: JobListing,
    scorer_output: ScorerOutput,
    resume_bullets: List[str],
    resume_text: str,
) -> str:
    """
    Rewrite resume bullets for this specific job, then generate the
    tailored resume file.
    Returns the path to the tailored resume file.
    """
    if not resume_bullets:
        print("[Tailor] No bullets found in resume. Returning base resume.")
        return settings.base_resume_docx

<<<<<<< HEAD
=======
    # Inject Extra Skills from Dashboard
    import os, json
    skills_path = os.path.join("memory", "skills.json")
    if os.path.exists(skills_path):
        with open(skills_path, "r", encoding="utf-8") as f:
            extra_skills = json.load(f).get("skills", "")
            if extra_skills.strip():
                resume_bullets.append(f"Additional capabilities and technical abilities: {extra_skills}")

>>>>>>> a135004 (Updated..)
    # Extract top 5 keywords from JD
    top_keywords = _extract_top_keywords(job.jd_text, scorer_output.matched_skills)

    prompt = TAILORING_PROMPT.format(
        bullet_list="\n".join(f"- {b}" for b in resume_bullets),
        matched_skills=", ".join(scorer_output.matched_skills[:10]),
        top_keywords=", ".join(top_keywords),
    )

    raw = await call_llm(prompt)
    tailored_bullets = _parse_bullet_response(raw, resume_bullets)

    print(f"[Tailor] Rewrote {len(tailored_bullets)} bullets for {job.company}.")

    # Edit the resume file
    output_path = await edit_resume(
        original_bullets=resume_bullets,
        tailored_bullets=tailored_bullets,
        company=job.company,
        job_title=job.job_title,
    )

    return output_path


def _extract_top_keywords(jd_text: str, matched_skills: List[str]) -> List[str]:
    """
    Extract the top 5 JD-specific keywords not already in matched_skills.
    Uses simple frequency counting.
    """
    tech_pattern = re.compile(
        r"\b(python|pytorch|tensorflow|transformers|bert|gpt|llm|nlp|rag|"
        r"langchain|docker|kubernetes|mlops|fastapi|sql|spark|airflow|"
        r"scikit.learn|huggingface|aws|gcp|azure|fine.tun\w+|embedding\w*|"
        r"vector\w*|retriev\w+|deploy\w+)\b",
        re.IGNORECASE,
    )
    found = tech_pattern.findall(jd_text)
    # Frequency count
    freq: dict[str, int] = {}
    for kw in found:
        freq[kw.lower()] = freq.get(kw.lower(), 0) + 1

    matched_lower = {s.lower() for s in matched_skills}
    # Prioritize keywords not already in matched_skills
    ranked = sorted(freq.items(), key=lambda x: -x[1])
    top = [kw for kw, _ in ranked if kw not in matched_lower][:5]
    if not top:
        top = [kw for kw, _ in ranked][:5]
    return top


def _parse_bullet_response(raw: str, original_bullets: List[str]) -> List[str]:
    """Parse LLM JSON array response. Falls back to originals on error."""
    raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
    try:
        bullets = json.loads(raw)
        if isinstance(bullets, list) and len(bullets) == len(original_bullets):
            return [str(b) for b in bullets]
        print(
            f"[Tailor] Bullet count mismatch: expected {len(original_bullets)}, "
            f"got {len(bullets)}. Using originals."
        )
        return original_bullets
    except json.JSONDecodeError as exc:
        print(f"[Tailor] JSON parse error: {exc}. Using original bullets.")
        return original_bullets
