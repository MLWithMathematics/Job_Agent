"""
scorer_agent.py
───────────────
LLM-based resume ↔ JD match scorer.

Key improvements over v1:
1. Separate scoring prompt for internships — does NOT penalise missing
   "years of experience" because internships don't expect that.
2. Separate match threshold (INTERNSHIP_MATCH_THRESHOLD < MATCH_THRESHOLD).
3. Soft-boost: if the JD says "fresher" / "0–1 year" and resume is a
   fresher profile, the score is boosted before thresholding.
4. Structured JSON response with `internship_fit` field so the log shows
   exactly why something was or wasn't approved.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List

from agents.search_agent import JobListing
from config import settings
from llm_client import call_llm


# ── Output model ──────────────────────────────────────────────────────────────

@dataclass
class ScorerOutput:
    score: int
    matched_skills: List[str]
    gaps: List[str]
    reasoning: str
    internship_fit: bool        # True if this is a good internship match
    status: str                 # 'approved' or 'skipped'


# ── Prompts ───────────────────────────────────────────────────────────────────

FULLTIME_PROMPT = """\
You are an expert technical recruiter evaluating a job application.

RESUME:
{resume_text}

JOB DESCRIPTION:
{jd_text}

Score how well this resume matches the JD on a scale 0–100.
Consider: required technical skills, domain relevance, project fit, \
tools and frameworks mentioned.
If the candidate is a fresher/student and the JD says 0–2 years experience \
or "entry level", do NOT penalise for lack of experience.

Return ONLY valid JSON (no markdown, no preamble):
{{
  "score": <int 0-100>,
  "matched_skills": [<matched keywords, max 10>],
  "gaps": [<missing requirements, max 5>],
  "reasoning": "<2–3 sentence summary>",
  "internship_fit": false
}}
"""

INTERNSHIP_PROMPT = """\
You are a senior technical recruiter evaluating an INTERNSHIP application.

IMPORTANT RULES FOR INTERNSHIP SCORING:
- Do NOT penalise for 0 years of industry experience. Internships are \
  for students and fresh graduates.
- DO reward: relevant personal projects, course projects, Kaggle/GitHub work, \
  academic coursework, hackathons, certifications.
- DO reward: understanding of core concepts even if not production-deployed.
- A score of 70+ means the candidate should interview. 
- A score of 50–70 means promising but gaps exist.
- Score below 50 only if there is a clear domain mismatch.

RESUME:
{resume_text}

INTERNSHIP JOB DESCRIPTION:
{jd_text}

Return ONLY valid JSON (no markdown, no preamble):
{{
  "score": <int 0-100>,
  "matched_skills": [<matched keywords/concepts, max 10>],
  "gaps": [<genuinely missing things, max 5>],
  "reasoning": "<2–3 sentence summary focusing on project/coursework fit>",
  "internship_fit": true
}}
"""

# ── Fresher boost signal words ────────────────────────────────────────────────

FRESHER_JD_SIGNALS = [
    "fresher", "0-1 year", "0–1 year", "entry level", "entry-level",
    "no experience required", "fresh graduate", "recent graduate",
    "final year", "2024 batch", "2025 batch", "pursuing",
]

FRESHER_RESUME_SIGNALS = [
    "b.tech", "b.e.", "msc", "m.tech", "bsc", "pursuing", "expected graduation",
    "cgpa", "gpa", "coursework", "academic project",
]


def _is_fresher_jd(jd_text: str) -> bool:
    jd_lower = jd_text.lower()
    return any(sig in jd_lower for sig in FRESHER_JD_SIGNALS)


def _is_fresher_resume(resume_text: str) -> bool:
    res_lower = resume_text.lower()
    return any(sig in res_lower for sig in FRESHER_RESUME_SIGNALS)


# ── Main scoring function ─────────────────────────────────────────────────────

async def run_scorer_agent(job: JobListing, resume_text: str) -> ScorerOutput:
    """
    Score a job/internship listing against the resume.
    Uses the correct prompt and threshold depending on `job.is_internship`.
    """
    if not job.jd_text.strip():
        print(f"[Scorer] Empty JD for {job.company}. Skipping.")
        return _make_skipped("No job description available.")

    # Choose prompt
    if job.is_internship:
        prompt = INTERNSHIP_PROMPT.format(
            resume_text=resume_text[:6000],
            jd_text=job.jd_text[:4000],
        )
        threshold = settings.internship_match_threshold
        label = "Internship"
    else:
        prompt = FULLTIME_PROMPT.format(
            resume_text=resume_text[:6000],
            jd_text=job.jd_text[:4000],
        )
        threshold = settings.match_threshold
        label = "Job"

    raw = await call_llm(prompt)
    output = _parse_response(raw)

    # Fresher soft-boost: if both JD and resume show fresher signals, boost by up to 8 pts
    if _is_fresher_jd(job.jd_text) and _is_fresher_resume(resume_text):
        boost = 8
        old_score = output.score
        output.score = min(100, output.score + boost)
        if boost and old_score != output.score:
            print(f"[Scorer] Fresher boost applied: {old_score} → {output.score}")
            output.reasoning += f" (Fresher profile +{boost} boost applied.)"

    # Apply threshold
    output.status = "approved" if output.score >= threshold else "skipped"

    print(
        f"[Scorer] [{label}] {job.company} | {job.job_title} → "
        f"Score: {output.score} | Threshold: {threshold} | {output.status.upper()}"
    )

    if output.status == "skipped":
        print(f"  └─ Gaps: {', '.join(output.gaps[:3]) or 'none listed'}")
    else:
        print(f"  └─ Matched: {', '.join(output.matched_skills[:5])}")

    return output


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_response(raw: str) -> ScorerOutput:
    """Parse LLM JSON. Falls back to regex on parse error."""
    raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()

    try:
        data = json.loads(raw)
        return ScorerOutput(
            score=max(0, min(100, int(data.get("score", 0)))),
            matched_skills=data.get("matched_skills", []),
            gaps=data.get("gaps", []),
            reasoning=data.get("reasoning", ""),
            internship_fit=bool(data.get("internship_fit", False)),
            status="pending",
        )
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[Scorer] JSON parse error: {exc}. Trying regex fallback.")
        score_match = re.search(r'"score"\s*:\s*(\d+)', raw)
        score = int(score_match.group(1)) if score_match else 0
        return ScorerOutput(
            score=score,
            matched_skills=[],
            gaps=[],
            reasoning="Score extracted via fallback (JSON parse failed).",
            internship_fit=False,
            status="pending",
        )


def _make_skipped(reason: str) -> ScorerOutput:
    return ScorerOutput(
        score=0,
        matched_skills=[],
        gaps=[reason],
        reasoning=reason,
        internship_fit=False,
        status="skipped",
    )
