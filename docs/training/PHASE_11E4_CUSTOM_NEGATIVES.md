# Phase 11E.4 — Custom Negatives Rationale (Step 5)

Target Phrase: `"Veronica"`

The official `openwakeword.train` pipeline uses `generate_adversarial_texts` to automatically construct hundreds of partial-phrase, prefix/suffix, and word-swap negatives. In addition to the automatic synthetic generation, the following explicit `custom_negative_phrases` are configured:

1. `"veronica is not here"`:
   - **Rationale**: Tests whole-sentence containment where "Veronica" appears as a component of a longer conversational sentence, verifying that the detector does not trigger inappropriately on conversational references without wake-word intent.
2. `"verona"`:
   - **Rationale**: Phonetic near-neighbor (Italian city name) sharing the onset and vowel qualities `[vɛˈroʊnə]` to harden the model against partial geographic name matches.
3. `"veronique"`:
   - **Rationale**: Direct lexical and phonetic cognate (French/continental variant) sharing the first three syllables but differing in final coda/accentuation.
4. `"veroni"`:
   - **Rationale**: Truncated prefix test clip ensuring the acoustic model requires the full trisyllabic/quadrisyllabic cadence before reaching threshold activation.
5. `"vero niko"`:
   - **Rationale**: Compound word boundary / split-pronunciation edge case to ensure proper continuous phoneme integration.

Arbitrary or unrelated word lists (e.g. unrelated English vocabulary) are omitted, relying instead on openWakeWord's background audio dataset and the automated synthetic adversarial generator for general negative distribution.
