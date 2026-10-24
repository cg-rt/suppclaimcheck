---
name: SuppClaimCheck
description: A calm stack of friendly cards that shows what the research says about one supplement claim.
colors:
  ground: "#eef0ea"
  card: "#ffffff"
  card-quiet: "#f6f7f3"
  ink: "#22261f"
  ink-2: "#5b6157"
  line: "#dde0d7"
  line-strong: "#8b9185"
  action: "#c2410c"
  action-hover: "#9a3412"
  action-tint: "#fbeee6"
  supported: "#3c6b2a"
  supported-tint: "#e9f1e4"
  contradicted: "#9b2c2c"
  contradicted-tint: "#f6e7e5"
  mixed: "#7a5a00"
  mixed-tint: "#f6efdc"
  none: "#5b6157"
  none-tint: "#eceee8"
  warning-tint: "#fdf3e2"
typography:
  title:
    fontFamily: "Nunito Sans, Segoe UI, sans-serif"
    fontSize: "2rem"
    fontWeight: 800
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  claim:
    fontFamily: "Nunito Sans, Segoe UI, sans-serif"
    fontSize: "1.625rem"
    fontWeight: 800
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  lede:
    fontFamily: "Nunito Sans, Segoe UI, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontFamily: "Nunito Sans, Segoe UI, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Nunito Sans, Segoe UI, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 700
    lineHeight: 1.3
  quote:
    fontFamily: "Source Serif 4, Georgia, serif"
    fontSize: "1.1875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  card: "20px"
  card-phone: "16px"
  field: "12px"
  pill: "999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "24px"
  "6": "32px"
  "7": "48px"
components:
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.card}"
    rounded: "{rounded.pill}"
    padding: "12px 24px"
    height: "48px"
  button-primary-hover:
    backgroundColor: "{colors.action-hover}"
    textColor: "{colors.card}"
  button-secondary:
    backgroundColor: "{colors.card}"
    textColor: "{colors.action}"
    rounded: "{rounded.pill}"
    padding: "12px 24px"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "24px 32px"
  card-quiet:
    backgroundColor: "{colors.card-quiet}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
  card-warning:
    backgroundColor: "{colors.warning-tint}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
  input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "12px 16px"
    height: "48px"
---

# Design System: SuppClaimCheck

The source of truth is `services/web/src/scc_web/static/css/app.css`; this file describes it. The world was chosen by the owner in the M3 visual-direction round ("Warm consumer app").

## Overview

**Creative North Star: "One question per card"**

A check reads as a short, calm column of white cards on a soft sage ground. Each card answers one question in plain words: what you asked, what the research says, the sentences themselves, what was searched, what wasn't. The interface is friendly and familiar so the evidence can be the interesting part.

**Key Characteristics:**
- A single 720px column on every screen; no sidebars.
- Soft-cornered white cards with one gentle shadow.
- One orange, and it only ever marks something you can press.
- Our voice is a rounded humanist sans; the literature's voice is an italic serif.
- Verdict colours are inks for words and marks, never fills that shout.

## Colors

### Primary
- **Action orange** (`action`): buttons, links, focus rings and the open/close summaries. Hover deepens to `action-hover`; `action-tint` is the selection colour and the soft focus halo on fields.

### Neutral
- **Sage ground** (`ground`): the page field. **Card white** (`card`) for every card; **quiet card** (`card-quiet`) for secondary cards, count tiles and the current progress row.
- **Ink** (`ink`) for text; **ink 2** (`ink-2`) for labels, hints and metadata (6.4:1 on white).
- **Lines** (`line`, `line-strong`): hairline separators inside cards and field borders.

### Verdict inks
- **Supported** green, **Contradicted** red, **Mixed** amber, **None** grey (Insufficient evidence, Not found). Each has a pale tint used only behind quotes of that direction. All inks pass 4.5:1 on white and on their tint.
- **Warning tint** (`warning-tint`) is reserved for the not-medical-advice card.

**The Pressable Orange Rule.** If it is orange, it can be pressed; if it can be pressed, its primary state is orange. Never use orange for a verdict, a status or decoration.

**The Calm Verdict Rule.** A verdict is a coloured word and a mark on a flat bar, never a coloured badge, gauge or ring.

## Typography

**Nunito Sans** (variable, self-hosted, weights 400–800) for everything we write. **Source Serif 4 Italic** (self-hosted) only for sentences quoted from an article or from the user's own text.

### Hierarchy
- **Title** (800, 2rem): one per page, the question the page asks.
- **Claim** (800, 1.625rem): the confirmed claim on the result page.
- **Lede** (400, 1.25rem): the sentence under a title; max 62ch.
- **Body** (400, 1.0625rem / 1.55).
- **Card label** (700, 0.875rem, `ink-2`): the heading of each card; it is the card's heading, not a label above another heading.
- **Quote** (Source Serif 4 italic, 1.1875rem): max 65ch, with curly quotes added by CSS.

**The Two Voices Rule.** The serif is the literature talking. Never set our own copy in it.

**The Tabular Rule.** Scores, counts, dates, PMIDs and times use tabular lining figures (`.num`).

Phones step the title to 1.625rem and the claim to 1.375rem. The scale is fixed rem, not fluid.

## Layout

- One column, 720px wide (480px for sign-in, account and errors), 32px side gutters on desktop and 16px at 640px and below. The header row is aligned to the same column.
- Cards stack with a 16px gap (12px on phones). Inside a card, groups sit 16px apart.
- At 640px and below: the header puts New check beside the name and the links on a second row; three-up field rows and four-up count tiles collapse to one and two columns; definition lists stack; primary buttons go full width.
- No horizontal scrolling at 320px.

## Elevation & Depth

Two levels only: the ground, and a card lifted by one soft shadow (`0 2px 10px` at 6% ink, plus a 1px contact shadow). Quiet and warning cards sit flat, with a hairline border for quiet ones. Nothing else casts a shadow.

## Shapes

Generously rounded: cards 20px (16px on phones), fields, notices, tiles and quotes 12px, buttons fully pill-shaped. Icons are drawn line icons on a 16px grid with round caps, one stroke weight, sized by the text beside them.

## Components

### Buttons
Pill, 48px tall (40px small in the header), white text on action orange, 700 weight. Hover deepens; pressed nudges down 1px; focus shows a 3px orange ring offset 3px. The secondary button is an orange outline that fills with `action-tint` on hover.

### Cards / Containers
- **Card:** white, 20px corners, soft shadow, 24px × 32px padding, its own small grey heading.
- **Quiet card:** `card-quiet` with a hairline border, no shadow. For context the user can skip.
- **Warning card:** `warning-tint`, amber heading. Only for the not-medical-advice notice.

### Inputs / Fields
48px tall, 12px corners, 1.5px `line-strong` border; on focus the border turns orange with a 3px `action-tint` halo. Labels sit above in 700; hints below the label in `ink-2`; "optional" is a small grey word after the label.

### Navigation
A plain header row: the name in 800 at left, text links (History, Account, Sign out), then the New check pill. The current page gets a 2px ink underline.

### Live progress list
One row per stage: a drawn mark (check for done, filled dot for current, ring for waiting), the stage name, and the time it started. The current row sits on `card-quiet` in bold, and its dot pulses gently unless reduced motion is set. Changes are announced through a polite live region.

### Verdict card
Verdict word (800, 2rem, verdict ink) with the score "67 out of 100" on the right; a one-sentence explanation; a flat 0–100 bar with a midline at 50 and a mark in verdict ink; four count tiles; "How the score was worked out" opens the rubric arithmetic.

### Evidence quote
A 12px-rounded block tinted by direction: the quote in the serif, the reason in our voice, then section, citation and licence in small grey.

## Do's and Don'ts

### Do:
- Do give each card one question and a heading that names it.
- Do put limits (what was searched, skipped and dropped) in the same stack as the verdict.
- Do keep the not-medical-advice card directly after the verdict.

### Don't:
- Don't use orange for anything that can't be pressed.
- Don't show the score as a ring, gauge or big hero number.
- Don't nest cards inside cards; inner groups are tiles or quote blocks.
- Don't set our own copy in the serif, or quotes in the sans.
- Don't load fonts or scripts from another host; the content security policy forbids it.
