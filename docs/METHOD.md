# How SuppClaimCheck reaches a verdict

The text for the method page (P3.11). It describes pipeline v1 with rubric v1 (P2.04, P2.05; D-044). The language model reads; code decides.

## In one paragraph
We search PubMed for open-access studies about the claim, pick the strongest kinds of study first, and read up to five of them. A language model labels passages as supporting the claim, contradicting it, or not relevant, and copies a sentence as proof. Code checks every copied sentence word for word against the article and throws away any that don't match. Code then weighs each article by its type of study and works out the verdict and a 0–100 score. The model never decides the verdict or the score.

## 1. Finding articles
- The claim you confirm becomes a PubMed search, built by code from the supplement, the effect and the population.
- Only open-access articles whose licence allows text mining can be read. Others are listed as "found, not assessable".
- Up to 20 matches are considered. Code sorts them by **type of study** (below), strongest first; PubMed's own relevance order breaks ties. The first five that can be read are read in full.

## 2. Types of study and their weight
Code reads each article's PubMed publication types and subject headings (MeSH) and puts it in one group. Recent articles not yet indexed have no headings; then a few words in the title (such as "rats" or "patients") decide, and failing that the article is "type unknown".

| Type of study | Weight | Counts as human evidence |
|---|---|---|
| Systematic review or meta-analysis | 1.0 | yes |
| Randomised controlled trial | 0.8 | yes |
| Other human trial | 0.6 | yes |
| Observational study | 0.4 | yes |
| Human study, design not stated | 0.4 | yes |
| Narrative review | 0.3 | yes |
| Study type unknown | 0.2 | no |
| Animal study | 0.15 | no |
| Lab study (cells) | 0.1 | no |
| Editorial, letter, case report, trial protocol | 0.1 | no |

The weights are versioned settings, not fixed rules; changing them needs an eval run (docs/EVALS.md). Sample size is not used yet: PubMed's metadata doesn't give it.

## 3. Reading each article
- Each article is split into passages; the passages that best match the claim's words go to the model, one article at a time.
- For each passage the model gives a label (supports, contradicts, not relevant), an exact quote and a one-sentence reason. "Contradicts" includes a finding that the supplement does the opposite of what is claimed.
- Every quote is checked against the passage, character for character (docs/QUOTE-VERIFICATION.md). A quote that fails is dropped and logged; it never counts and is never shown.

## 4. From quotes to a verdict
For each article, its **stance** is (quotes for − quotes against) ÷ (all its verified quotes): +1 if all agree with the claim, −1 if all disagree. An article with no verified quote has no finding and doesn't count. A long article with many quotes counts once, like a short one.

Across the articles with a finding:
- **Direction** = the weight-averaged stance, from −1 (against) to +1 (for).
- **Strength** = their total weight ÷ 2.0, at most 1. Two systematic reviews give full strength; one narrative review gives 0.15.
- **Score** = 50 + 50 × direction × strength. 0 means consistent strong evidence against, 50 balanced or too weak to call, 100 consistent strong evidence for (D-002).

The **verdict** is the first rule that applies:

| # | Rule | Verdict |
|---|---|---|
| 1 | No article could be read | Not found |
| 2 | No article has a verified finding | Insufficient evidence |
| 3 | The only findings come from animal, lab or unclassified studies | Insufficient evidence |
| 4 | Strength is below 0.25 | Insufficient evidence |
| 5 | The smaller side holds at least 25% of the weighted evidence | Mixed |
| 6 | Otherwise | Supported or Contradicted, by the direction |

**Worked example.** A meta-analysis supports the claim (weight 1.0), a randomised trial supports it (0.8), a narrative review contradicts it (0.3). Weight for 1.8, against 0.3: the smaller side is 14% of 2.1, under 25%, so not Mixed. Direction = (1.8 − 0.3) ÷ 2.1 = +0.71; strength = 2.1 ÷ 2.0, capped at 1. Score = 50 + 50 × 0.71 × 1 = 86: **Supported**.

## 5. Limits
- Only open-access articles are read, and at most five per claim. Strong evidence behind a paywall is missed.
- Labelling passages is the model's judgement and can be wrong; the eval set measures how often (docs/EVALS.md). Quotes are always verified; labels are not.
- Study type comes from metadata, which can be missing or coarse. A "human study, design not stated" may be anything from a trial to a survey.
- The verdict describes what the articles read say about the claim as confirmed. It is not medical advice, and it says nothing about dose or safety for you.
