# swiszRouter: learned deterministic routing for swiszCLI (2026-06-01)

Status: draft v1 (sibling to memory-ideas doc)

Goal: bypass the LLM for the majority of Sean recurring intents. Route deterministically, zero tokens spent. LLM fires only for novel asks. This is how a 9B model starts feeling like Claude Code -- for well-trodden paths there is no model in the loop at all.

Sits between user input and swiszard. NOT a replacement for the LLM in open conversation; the LLM still narrates, still handles novel reasoning, still teaches the router.


THE ONE-TAP LEARNING PATTERN
============================

Ambiguous signal triggers a single yes-or-no prompt in the CLI. Sean answer plus the original signal get stored as a labeled example. After N consistent examples of the same shape, the deterministic classifier takes over and stops asking. LLM is bootstrapper, not runtime.

Storage: learned_patterns table:
  id, pattern_kind, pattern_regex, target_template, weight,
  wins, losses, last_used, source

source values: seed, user_confirmed, llm_proposed, github_shared


WHERE ONE-TAP LEARNING APPLIES
==============================

1. CORRECTIONS (memory-ideas Q5, Sean directive 2026-06-01)
   was that a correction? Save pattern.
2. GRATITUDE / SUCCESS SIGNALS
   chunks following gratitude get a quality boost in retrieval.
3. SARCASM FLAGGING (Sean directive memory:618)
4. INTENT DISAMBIGUATION on short messages -- pick from top 3 guesses.
5. QUESTION VS INSTRUCTION classification for ambiguous one-liners.
6. SAVE-WORTHY MOMENTS -- prompt asks save this? when prefs/facts stated.


ROUTING CASCADE
===============

step 1. Run all regexes against user input.
step 2. Each match -> (target_template, score) where
        score = weight * specificity * recency_boost.
step 3. Top match above threshold T -> execute deterministically. No LLM.
step 4. Below threshold -> fall to LLM, let it propose target,
        then prompt user with single confirmation.
step 5. Confirm -> save generalized regex + target with weight=0.5,
        source=user_confirmed.
step 6. Next time similar phrasing appears, cascade catches it.

Weight dynamics:
- successful match increments weight
- correction decrements
- after enough confirmations, deterministic shortcut wins

Generalization trick: the system does NOT store raw input as regex. It stores a generalized version (replace concrete nouns with capture groups, keep verb/intent words literal). One confirmation teaches dozens of phrasings.


WHY THIS IS A REAL ARCHITECTURE
===============================

Academic name: weighted finite-state transducer (WFST) with online learning. Built in the 90s before transformers ate the field. Works in BOUNDED domains. Caden IS bounded: intent -> swiszard DSL is finite, enumerable. A transformer is overkill for translating a phrase like find that python file with the wizard stuff into the swiszard DSL. Regex plus weights handles it.


GITHUB SHARING = FEDERATED LEARNING WITHOUT THE HORROR
=====================================================

Every user PatternStore can be opt-in shared. Patterns with high cross-user win-rate become seed weights for new installs. Just SQL row aggregation. Privacy trivial because regexes are abstract patterns, not personal data.


SIBLING IDEAS TO EXPLORE
========================

A. TINY ROUTING MODEL (Sean directive 2026-06-01)
   Alternative or supplement to regex+weights: train our own routing model, no bigger than nomic-embed-text. Options:
   - Token translator: tiny seq2seq that maps user phrasing -> canonical swiszard DSL string. Trained on the same (input, target) pairs the WFST collects. ~5-20M params.
   - Intent classifier: tiny encoder that maps user msg -> handler_id, fed by the same labeled pairs.
   - Embedding-router: reuse nomic-embed-text. Each handler has prototype embeddings (its trigger phrases). New input embedded, nearest prototype wins. Zero new model needed -- the cheapest option.
   Compare against the WFST after both have data; pick whichever is more accurate or compose them.

B. TEMPLATE LIBRARY EXTRACTION
   When the LLM proposes a target the user confirms, also extract the slot structure. Build a library of swiszard DSL templates with named slots. Router fills slots from user input via regex captures.

C. HANDLER PROTOTYPE EMBEDDINGS
   Cheapest tiny-model option above. Each swiszard handler ships with 3-5 example trigger phrases. Embed once at startup. Cosine-match user input to all prototypes. Top match above threshold -> direct route. Cheap as dirt, no training needed, ships day one.


STACK PLACEMENT
===============

input -> swiszContext recall -> swiszRouter cascade
            chunks injected           match? -> swiszard direct
                                      miss?  -> LLM -> swiszard
                                                -> confirm -> update swiszRouter

End state: 80% of Sean recurring intents routed deterministically, in microseconds, zero LLM tokens. LLM only fires for novel asks or open conversation.


OPEN QUESTIONS
==============

R1: WFST first, tiny model later? Or build prototype embeddings (sibling C) day one as the simplest version?
R2: One PatternStore per user, or per (user, project) so coding patterns dont leak into chat?
R3: Federated sharing -- opt-in upload to public registry, or peer-to-peer between users who follow each other?
R4: How does this compose with ATGC if/when ATGC ships? swiszRouter handles routing, ATGC handles semantic composition -- different layers. ATGC sits above swiszRouter in the stack, not in competition.
