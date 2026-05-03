from __future__ import annotations

from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────
    gemini_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    ollama_model: str = Field(default="mistral:7b")
    ollama_base_url: str = Field(default="http://localhost:11434")

    # ── LinkedIn ──────────────────────────────────────────────────────
    linkedin_email: str = Field(default="")
    linkedin_password: str = Field(default="")

    # ── Naukri ────────────────────────────────────────────────────────
    naukri_email: str = Field(default="")
    naukri_password: str = Field(default="")

    # ── Captcha ───────────────────────────────────────────────────────
    twocaptcha_api_key: str = Field(default="")

    # ── Matching thresholds ───────────────────────────────────────────
    match_threshold: int = Field(
        default=85,
        description="Minimum score to auto-apply for full-time jobs",
    )
    internship_match_threshold: int = Field(
        default=70,
        description=(
            "Lower threshold for internships. Internships often don't list years of "
            "experience so a slightly lower bar is correct."
        ),
    )

    # ── Search ────────────────────────────────────────────────────────
    search_keywords: str = Field(
        default="machine learning engineer,NLP engineer,AI engineer,MLOps",
        description="Comma-separated full-time job keywords",
    )
    internship_keywords: str = Field(
        default="machine learning intern,NLP intern,AI intern,data science intern,deep learning intern",
        description="Comma-separated internship search keywords",
    )
    search_location: str = Field(default="India")
    max_listings_per_run: int = Field(default=20)

    # ── Resume ────────────────────────────────────────────────────────
    resume_format: str = Field(default="docx", description="'docx', 'pdf', or 'latex'")
    base_resume_docx: str = Field(default="resume/base_resume.docx")
    base_resume_tex: str = Field(default="resume/base_resume.tex")
    base_resume_pdf: str = Field(default="resume/base_resume.pdf")
    tailored_resume_dir: str = Field(default="resume/tailored/")

    # ── Personal defaults ─────────────────────────────────────────────
    phone: str = Field(default="")
    current_location: str = Field(default="")
    notice_period: str = Field(default="")
    current_ctc: str = Field(default="")
    expected_ctc: str = Field(default="")
    total_experience_years: str = Field(default="")
    # Identity
    full_name: str = Field(default="")
    first_name: str = Field(default="")
    last_name: str = Field(default="")
    email: str = Field(default="")
    # Online profiles
    linkedin_url: str = Field(default="")
    github_url: str = Field(default="")
    portfolio_url: str = Field(default="")
    # Education
    college: str = Field(default="")
    degree: str = Field(default="")
    graduation_year: str = Field(default="")
    # Current position
    current_company: str = Field(default="")
    current_role: str = Field(default="")
    # Eligibility
    work_authorization: str = Field(default="Yes")
    gender: str = Field(default="")
    nationality: str = Field(default="")

    # ── Database ──────────────────────────────────────────────────────
    db_path: str = Field(default="memory/ledger.db")
    form_memory_path: str = Field(default="memory/form_memory.json")

    # ── Browser ───────────────────────────────────────────────────────
    headless: bool = Field(default=False)
    slow_mo: int = Field(default=50)
    viewport_width: int = Field(default=1366)
    viewport_height: int = Field(default=768)
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )

    # ── Naukri Refresh ────────────────────────────────────────────────
    naukri_refresh_interval_hours: int = Field(default=4)

    @property
    def keyword_list(self) -> List[str]:
        return [k.strip() for k in self.search_keywords.split(",") if k.strip()]

    @property
    def internship_keyword_list(self) -> List[str]:
        return [k.strip() for k in self.internship_keywords.split(",") if k.strip()]


settings = Settings()
