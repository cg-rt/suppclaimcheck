---
version: 1
slug: "services-web"
primary_target: "services/web"
related_targets: []
---

# Surface brief: public web app (services/web)

**Scope:** the signed-in product: sign in, new check, confirm claim, live progress, result, history, password change. Admin (P3.08–P3.10) inherits this world later.
**Mode:** Operate (the result page reads as Read).
**Audience and job:** evaluators on desktop first (judge care, honesty and build quality by running a claim and reading the result closely); invited non-expert testers on phones.
**Scene:** daytime, a laptop at a desk or a phone in hand after seeing an ad; ordinary room light. Light theme.
**Constraints:** WCAG 2.2 AA; 390 px, 320 px, desktop; strict CSP, self-hosted fonts and scripts; pages work without JavaScript except live progress; never "proven"/"cures"; not-medical-advice notice on every result.
**Memorable moment:** one calm column where each card answers one question, and the article's own sentences, set in their own serif, carry the evidence.
**Unresolved:** method and privacy pages (P3.06, P3.11); admin layout (P3.08); dark theme.

## Direction contract

THESIS: A check is a short stack of friendly cards, one question per card: what you asked, what the evidence says, the sentences themselves, what was searched, what wasn't. It refuses the fact-check dashboard of badges, gauges and a hero score.

OWN-WORLD: Sage-grey ground #eef0ea; white cards, 20px corners, one soft offset shadow; ink #22261f, secondary #5b6157. One orange #c2410c, only on things you can press (buttons, links, focus). Verdict inks never orange: green #3c6b2a, red #9b2c2c, amber #7a5a00, grey #5b6157. Nunito Sans for our voice, Source Serif 4 italic for the literature's. Tabular figures for numbers. Drawn line icons, one stroke.

STORY: The visitor sees what was checked, reads the evidence in its own words, sees what was searched and skipped, and trusts the result because its limits sit in the same stack.

FIRST VIEWPORT: Plain header row: name left; History, Account, Sign out, then the orange pill New check right. A 720px column: claim card (claim at 26px), verdict card (verdict word, score out of 100 on a flat 0–100 bar, one-line why, four counts), then the not-medical-advice card.

FORM: Warm consumer app surface, the competitive challenger, chosen by the owner on the decision page over the rolled leaflet. seed:0d9ab32a50b08fb6. Kept from the round: tabular figures; a changed progress stage holds its highlight; quotes in their own face.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
