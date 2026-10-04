# AI Usage — Key Prompts, Decisions, and Corrections

A curated record of how AI tools were used in this project: what they produced, what I accepted, changed, or rejected, and why. Raw session logs are not included (confirmed with Incubyte).

## Tools
| Tool | Used for |
|---|---|
| Claude (chat) | Requirements analysis, clarifying questions, scope decisions, design review |
| _[agentic coding tool]_ | Implementation, tests, refactoring |

Instructions given to the coding agent are committed in _[e.g. `CLAUDE.md`]_.

## How I Worked with AI
- Requirements and scope were settled before any code was written; early scaffold code generated before scope was confirmed was discarded.
- AI suggestions were checked against the assessment brief and Incubyte's clarifications, not accepted on plausibility.
- _[Add: test-first workflow, how generated code was reviewed, how correctness was verified]_

---

## Log

### Phase 1 — Requirements

**1. Clarifying questions overstated the brief**
- **Prompt:** Asked AI to draft clarifying questions about the assessment.
- **AI output:** Question framed the app as being for a "single HR Manager."
- **Correction:** The brief says "HR Manager," not "single." The question presumed an answer to the very thing I was asking. Rewritten neutrally.
- **Lesson:** AI paraphrase can quietly add assumptions; compare against the source wording.

**2. Question presumed a feature the brief never asked for**
- **AI output:** Asked what spreadsheet format the "import feature" should support.
- **Correction:** The brief only says data currently lives in Excel; import was the AI's inference. Reworded to ask whether migrating existing data is in scope. Incubyte confirmed it is optional.
- **Decision:** I included CSV import anyway, as the migration path off Excel, with all-or-nothing validation.

**3. AI recommended asking fewer questions**
- **AI output:** Suggested asking only the two questions that block design, defaulting the rest to save time.
- **Decision:** The brief explicitly says "do not make assumptions or proceed with doubts," so I sent the full set. Incubyte's answers changed scope: no authentication, structured analytics instead of AI Q&A, import optional.

**4. Comprehensive spec vs. one-page constraint**
- **Prompt:** Asked AI to make the requirements doc more comprehensive.
- **AI output:** Flagged that the brief asks for a one-page document.
- **Decision:** Split into a one-page summary (Part 1) and a detailed specification (Part 2) in the same file.

**5. Static vs. live exchange rates**
- **AI output:** Proposed a static exchange-rate table for deterministic analytics.
- **Decision:** I chose live rates, since HR needs current costs. AI identified the consequences: figures change without salary changes, dependency on an external API, and non-deterministic tests.
- **Resolution:** Rates fetched daily and stored; last stored rates used if a fetch fails; every analytics view shows "rates as of [date]"; tests use fixed rates and never call the API.

**6. Employees changing country**
- **AI output:** Assumed each employee has one country and is paid in its currency.
- **Correction:** Country can change. Country and currency moved onto each compensation record, so relocation adds a new record and history stays intact. Cross-currency increase reporting is planned separately.

**7. Deployment requirement**
- **My assumption:** Sharing the repo link meant deployment wasn't needed.
- **AI output:** Pointed to "Readiness: fully functional deployed software" in the brief, a separate requirement from the submission method.
- **Decision:** Deploy the 10,000-employee app on free tiers; live URL and demo video at the top of the README.

### Phase 2 — Design and Scale

**8. In-memory analytics would not scale**
- **AI output:** An early scaffold loaded all current salaries into Python to compute medians and group statistics.
- **Correction:** Acceptable at 10,000 rows, but it breaks at 10 lakh. Moved to SQL-side aggregation (`percentile_cont`), a denormalised current-compensation table, keyset pagination, and PostgreSQL instead of SQLite.
- **Follow-up:** Built a scale lab to test these decisions on 1M and 10M synthetic records (`docs/SCALE_LAB.md`).

### Phase 3 — Implementation
_[Add entries as you build: what the agent generated, what you changed, bugs it introduced, tests that caught them.]_

---

## Entry Template
**N. Short title**
- **Prompt:** what I asked
- **AI output:** what it produced
- **Correction / Decision:** what I changed or chose, and why
- **Lesson:** (optional) what I'd do differently
