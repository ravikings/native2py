---
name: deai-blog-editor
description: Edit or review any user-facing text (blog posts, articles, UI copy, marketing copy, emails) to strip out common "AI writing" tells (based on Wikipedia's "Signs of AI writing" field guide) so it reads as natural human writing. Use this whenever the user asks to "de-AI" text, make writing sound more human, review a draft for AI-sounding language, edit a blog before publishing, or whenever writing/reviewing any text for the app.
---

# De-AI Blog Editor

A checklist-driven editing pass for catching the patterns that make writing look AI-generated, adapted from Wikipedia's WikiProject AI Cleanup "Signs of AI writing" guide. Apply this whenever reviewing or editing any draft — blog post, UI copy, marketing text, email — even if it wasn't originally written with AI, checking against this list makes prose read more human and specific.

Use this skill proactively for any text we write for the app (buttons, empty states, headers, tooltips, emails), not just blog posts.

## How to use this skill

1. Read the full draft first.
2. Run it against every category below. For each hit, quote the offending phrase/structure and propose a specific rewrite — don't just flag it, fix it.
3. Prioritize: language/tone and style issues first (most visible to readers), then structure, then meta-content.
4. Give the user a short summary of what changed and why, then the edited draft.
5. Don't over-correct into terse, personality-free prose — the goal is specific and human, not stripped down.

## Checklist

### Language & tone
- Generic, inflated praise: "rich cultural heritage," "stands as a testament to," "plays a vital/significant role," "in the world of X." Replace with the actual specific fact instead of the vague honorific.
- Overuse of "boosting," "enhancing," "unlocking," "leveraging," "delving into," "navigating," "underscoring," "highlighting" — cut or use plainer verbs.
- False/negative parallelism: "It's not just X, it's Y." Use sparingly — one per whole piece, max.
- Editorializing/vague attribution: "critics argue," "some believe," "it is important to note that." Name the actual source or cut the sentence.
- Excessive hedging or false balance on things that don't need it.
- Repetitive summary/transition phrases: "In summary," "Overall," "In conclusion," "It is worth noting." Usually just delete the sentence they introduce.
- Overuse of "moreover," "furthermore," "additionally" as paragraph openers.

### Style & formatting
- Rigid formulaic structure — every section the same length/shape, every paragraph a topic sentence + 3 generic supporting points.
- Excessive bolding of random phrases, or title-casing casual headers.
- Overused em dashes as a crutch for connecting clauses — vary sentence structure instead.
- Bullet-list overload where prose would read more naturally.
- Emoji in headers or as bullet decoration (rare in real blog writing outside specific niches).
- Suspiciously uniform paragraph lengths.

### Substance
- Statistical smoothing: vague claims that could apply to almost any topic ("faces various challenges," "has evolved over time"). Replace with the one true specific fact only this subject has.
- Puffed-up "broader significance" statements connecting a small detail to a sweeping trend without evidence.
- Claims with no verifiable source, or a source that doesn't actually say that.

### Leftover artifacts (check these especially if the user drafted with AI help)
- Unedited placeholder/instruction text ("[insert stat here]," "as an AI language model").
- Leftover meta-commentary addressed to the user rather than the reader ("Let me know if you'd like me to expand this section").
- Curly quotes/apostrophes inconsistent with the rest of the site's style, if relevant.

## Output format

For each fix, use:

> **Original:** "..."
> **Issue:** [category]
> **Suggested:** "..."

Then provide the full cleaned draft at the end.
