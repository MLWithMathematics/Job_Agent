# 🤖 Agentic Job Application System

A fully autonomous multi-agent pipeline that searches LinkedIn & Naukri for jobs,
scores them against your resume using an LLM, tailors your resume per application,
auto-applies via Playwright, sends recruiter outreach, and keeps a Streamlit dashboard.

---

## 📁 Project Structure

```
job_agent/
├── main.py                  # Entry point — runs the full LangGraph pipeline
├── config.py                # Pydantic settings (reads from .env)
├── llm_client.py            # Unified Gemini + Ollama LLM client
├── agents/
│   ├── search_agent.py      # Scrapes LinkedIn & Naukri listings
│   ├── scorer_agent.py      # LLM-based resume ↔ JD match scoring
│   ├── tailoring_agent.py   # LLM bullet rewriter + resume editor
│   ├── apply_agent.py       # Playwright browser automation
│   ├── outreach_agent.py    # LinkedIn recruiter connection sender
│   └── refresh_agent.py     # Naukri profile refresh scheduler
├── memory/
│   ├── form_memory.py       # Fuzzy-match form field memory (JSON)
│   └── ledger.py            # SQLite application log
├── browser/
│   ├── external_flow.py     # Generic external job application form filler
│   ├── linkedin_flow.py     # LinkedIn Easy Apply handler
│   ├── naukri_flow.py       # Naukri apply + profile refresh
│   ├── popup_handler.py     # Multi-strategy popup & modal suppression
│   └── stealth.py           # Human behavior simulation & CAPTCHA handling
├── resume/
│   ├── base_resume.docx     # ← YOUR MASTER RESUME (Word) — place here
│   ├── base_resume.tex      # ← YOUR MASTER RESUME (LaTeX) — optional
│   ├── resume_editor.py     # Docx/LaTeX tailoring + PDF compile
│   └── tailored/            # Auto-generated tailored resumes saved here
├── dashboard/
│   └── app.py               # Streamlit application ledger
├── requirements.txt
├── .env.example
└── README.md
```

---

## ✨ New Features in v3

- **External Portals & CAPTCHA Handling:** Introduces `external_flow.py` for automatically detecting and filling complex job applications on non-platform company portals. Built-in human-behavior simulation (`stealth.py`) handles captchas via external solvers or pauses execution for manual verification.
- **Continuous Popup Suppression:** A robust, background `PopupHandler` constantly dismisses overlays, notification prompts, and modals without interrupting the main automation flows.
- **Expanded Resume Support:** Native text and list extraction from PDFs using `pdfminer.six` and `pypdf`, complementing the existing docx and LaTeX tailors.
- **Internship Prioritization:** Configurable logic for scoring internships versus standard jobs, maximizing match relevance (approves automatically on threshold).

---

## ⚙️ Setup Instructions

### 1. Prerequisites

- Python 3.10+
- (Optional) LaTeX — for PDF compilation: install [MiKTeX](https://miktex.org/) (Windows) or `texlive` (Linux/macOS)
- (Optional) Ollama — for local LLM fallback

### 2. Create & activate virtual environment

```bash
cd job_agent
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
playwright install-deps chromium   # Linux only
```

### 5. Install Ollama (fallback LLM — optional but recommended)

**Option A — Ollama (local, free, offline):**
```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows — download installer from https://ollama.com/download

# Pull the fallback model
ollama pull mistral:7b
```

**Option B — Skip Ollama entirely:**
Set `GEMINI_API_KEY` in `.env` and Ollama will only be used as a fallback if Gemini fails.
If you have no Ollama and Gemini fails, the pipeline will raise an error.

---

## 🔑 Configure Environment Variables

```bash
cp .env.example .env
```

Open `.env` and fill in **all** required fields:

```dotenv
# Required
GEMINI_API_KEY=your_key_here        # Get from https://aistudio.google.com
LINKEDIN_EMAIL=you@email.com
LINKEDIN_PASSWORD=yourpassword
NAUKRI_EMAIL=you@email.com
NAUKRI_PASSWORD=yourpassword

# Your personal info (pre-fills form fields automatically)
PHONE=+91XXXXXXXXXX
CURRENT_LOCATION=Bangalore, India
NOTICE_PERIOD=30 days
CURRENT_CTC=0 LPA
EXPECTED_CTC=12 LPA
TOTAL_EXPERIENCE_YEARS=1

# Tuning
MATCH_THRESHOLD=85                  # Jobs scoring below this are skipped
SEARCH_KEYWORDS=machine learning engineer,NLP engineer,AI engineer
SEARCH_LOCATION=India
MAX_LISTINGS_PER_RUN=20

# Optional
TWOCAPTCHA_API_KEY=                 # Only needed if you hit CAPTCHAs often
RESUME_FORMAT=docx                  # 'docx' or 'latex'
```

---

## 📄 Place Your Resume

1. **Word format (recommended):**
   Place your resume as: `resume/base_resume.docx`

   > The agent reads bullet points from this file, rewrites them per JD,
   > and saves tailored copies in `resume/tailored/`.
   > 
   > **Format tip:** Use standard Word bullet lists (Home → Bullets).
   > The extractor detects paragraphs with list formatting.

2. **LaTeX format (optional):**
   Place your `.tex` source as: `resume/base_resume.tex`
   Set `RESUME_FORMAT=latex` in `.env`.
   Requires `pdflatex` installed and on your PATH.

3. **PDF format (supported for read-only / extraction):**
   Place your resume as: `resume/base_resume.pdf`
   > The agent can extract text and bullet points from PDFs using `pdfminer.six` or `pypdf`. Note that dynamic tailoring output may fall back to Word format unless a LaTeX source is also provided.

---

## 🚀 Running the Application

### Run the full agent pipeline

```bash
python main.py
```

The pipeline will:
1. **Search** LinkedIn & Naukri for jobs matching your keywords
2. **Score** each job against your resume via Gemini
3. **Skip** jobs below `MATCH_THRESHOLD`
4. **Tailor** your resume bullets for approved jobs
5. **Apply** via Playwright browser automation
6. **Send outreach** to recruiters if found
7. **Log** everything to SQLite

> ⚠️ The browser window will open (headful mode). Do not click in it while running.
> If you need to solve a CAPTCHA, the agent will pause and prompt you in the terminal.

---

### Run the Streamlit dashboard

```bash
streamlit run dashboard/app.py
```

Opens at `http://localhost:8501`. Shows:
- Total applied / avg score / platform breakdown
- Applications per day (14-day bar chart)
- Status breakdown pie chart
- Score distribution histogram
- Full searchable, filterable table with "Mark as Interview" buttons

---

### Run the Naukri refresher standalone

```bash
python agents/refresh_agent.py
```

Starts APScheduler and refreshes your Naukri profile every 4 hours (configurable via `NAUKRI_REFRESH_INTERVAL_HOURS` in `.env`).
Run this in a separate terminal alongside `main.py`, or in the background:

```bash
# Background (Linux/macOS)
nohup python agents/refresh_agent.py &

# Windows (separate terminal)
start python agents/refresh_agent.py
```

---

## 🗂️ File Placement Reference

| File | Where to place |
|------|---------------|
| Your Word resume | `resume/base_resume.docx` |
| Your LaTeX resume | `resume/base_resume.tex` |
| Environment config | `.env` (copy from `.env.example`) |
| Form answers cache | `memory/form_memory.json` (auto-created) |
| Application DB | `memory/ledger.db` (auto-created) |
| Tailored resumes | `resume/tailored/` (auto-generated) |

---

## 🧠 How the Form Memory Works

The agent remembers answers to application form fields so you don't get asked the same question twice.

- Stored in `memory/form_memory.json`
- Pre-populated from your `.env` (phone, location, notice period, etc.)
- Uses fuzzy matching (85% similarity threshold) to match similar field labels
- If a **new** hard field appears (phone, salary, etc.) → the agent **pauses and asks you** in the terminal
- Your answer is saved for all future applications

---

## 🔧 Troubleshooting

| Problem | Fix |
|---------|-----|
| `base_resume.docx not found` | Place your resume at `resume/base_resume.docx` |
| Browser opens and immediately closes | Check LinkedIn/Naukri credentials in `.env` |
| Gemini quota exceeded | Gemini free tier: 15 req/min. The agent auto-falls back to Ollama |
| CAPTCHA blocking apply | Set `TWOCAPTCHA_API_KEY` in `.env`, or manually solve when prompted |
| `playwright install` fails | Run `playwright install-deps chromium` (Linux) or run as admin (Windows) |
| Low match scores | Lower `MATCH_THRESHOLD` in `.env` (e.g. to `75`) |
| Score always 0 | Check that `GEMINI_API_KEY` is valid and `resume/base_resume.docx` exists |

---

## 📦 Tech Stack

| Component | Library |
|-----------|---------|
| Pipeline orchestration | LangGraph (StateGraph) |
| Browser automation | Playwright (Chromium, headful) |
| Primary LLM | Gemini 1.5 Pro (google-generativeai) |
| Fallback LLM | Ollama + mistral:7b |
| HTML parsing | BeautifulSoup4 |
| Resume extraction & editing | python-docx / pdflatex / pdfminer.six / pypdf |
| Database | SQLite (built-in) |
| Dashboard | Streamlit + Plotly |
| Scheduler | APScheduler |
| Config | pydantic-settings |

---

## ⚠️ Ethical Note

This tool is intended for legitimate personal job searching. Use it responsibly:
- Do not apply to roles you are clearly unqualified for
- Do not falsify resume content (the LLM is instructed not to invent facts)
- Respect platform rate limits and terms of service
