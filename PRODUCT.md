# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary: evaluators on desktop.** People reading SuppClaimCheck as a portfolio project: engineers, product people and hiring teams, on a laptop, usually after following a link. They judge whether the product is careful, honest about its limits, and well built. They try a claim themselves and read the result closely.
- **Also: invited testers on phones.** A few non-experts who saw a supplement claim in an ad, article or video and want to know what the literature says. No science background. Pages must work fully at 390 px and 320 px.

## Product Purpose

Checks a claim about a dietary supplement against open-access biomedical literature. The user submits text, confirms the claim the system extracted, and gets a verdict (Supported, Contradicted, Mixed, Insufficient evidence, Not found), a 0–100 score with its breakdown, and verbatim, verified quotes with full citations. Success: a reader can see exactly what was searched, what was read, which sentences support or contradict the claim, and what was not checked.

## Positioning

Every quote shown has been checked word for word by code against the article it cites; quotes that fail are dropped, never shown. The verdict and score are computed by code from a versioned rubric, not by the language model. "Not found" and "Insufficient evidence" are valid, honest answers.

## Operating Context

- Invite-only accounts; no sign-up. A check takes about a minute and runs in stages (extracting, waiting for confirmation, searching, retrieving, assessing, verifying, scoring), shown live.
- Nothing is searched or spent before the user confirms the claim.
- Only open-access articles whose licence allows text mining are read. Relevant articles that couldn't be read are listed as found but not assessed.
- A monthly budget cap can refuse checks; the message must say so plainly.

## Capabilities and Constraints

- Server-rendered pages (FastAPI, Jinja2, htmx with server-sent events). Little JavaScript; pages must work without it except live progress.
- Input types now: text and pasted ad text. Later, behind flags: article URL, ad screenshot, pasted YouTube transcript.
- Undecided: the method page wording (P3.11), the privacy page (P3.06).

## Brand Commitments

- Name: SuppClaimCheck. Domain: suppclaimcheck.com. No logo, colours or fonts decided before this design.
- Voice: plain and careful. Short sentences. Say what was and wasn't checked. Never sound more certain than the evidence. Scientific detail available, not in the way.
- Product text never says "proven" or "cures", and never gives medical or dosing advice. Every result carries a not-medical-advice notice.

## Evidence on Hand

- Real pipeline output for a canned claim (creatine, resistance training, older adults) in `tests/fixtures/canned_claim/`.
- No testimonials, users, usage numbers or press exist. Do not invent any.

## Product Principles

1. Show the evidence, not an opinion: quotes, sections and citations come before any summary.
2. Honest limits are part of the answer: what was searched, what was skipped and why, every time.
3. The user stays in control of spending: confirmation before any search.
4. Calm under uncertainty: Mixed and Not found are presented as results, not errors.

## Accessibility & Inclusion

WCAG 2.2 AA. Keyboard-complete. Checked at 390 px, 320 px and desktop. Live progress must be announced to screen readers without flooding them.
