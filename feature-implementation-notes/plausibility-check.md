# LLM Plausibility Check — Implementation Notes

## What It Does
Runs an async LLM check (gpt-4o-mini) on every user turn to detect nonsensical STT output. If the check returns IMPLAUSIBLE, the agent interrupts and asks the user to repeat. Zero latency on the happy path — the check runs parallel to the main response pipeline.

## Architecture
```
User speaks → STT → Turn committed
                     ├── Main pipeline: EOU → LLM (gpt-4.1) → TTS → Agent speaks
                     └── Async check:   gpt-4o-mini → "Is this plausible?"
                                        ├── PLAUSIBLE → do nothing
                                        └── IMPLAUSIBLE → interrupt agent, ask to repeat
```

## Files
- `phone/src/stt/plausibility.py` — PlausibilityGuard class + check_plausibility function
- `phone/src/flows/voice.py` — wired into _setup_user_input_handler and _setup_transcript_handler

## How It Works
1. `_setup_transcript_handler` tracks what the agent last said → feeds to PlausibilityGuard
2. `_setup_user_input_handler` fires `plausibility_guard.on_transcript()` when a turn commits (listening → thinking)
3. `PlausibilityGuard.on_transcript()` creates an async task that calls gpt-4o-mini
4. gpt-4o-mini checks: conversation language + agent's last utterance + user transcript
5. If IMPLAUSIBLE: `session.say("Sorry, I didn't catch that...")` in the conversation language
6. If PLAUSIBLE or timeout (3s): do nothing

## What It Catches
- Language mismatch: Turkish call but STT returns English words
- Domain mismatch: "biomarkers" in a banking conversation
- Garbled fragments: random syllables that don't form coherent speech
- Profanity artifacts: STT hearing profanity from non-profane speech

## What It Doesn't Catch
- Minor word errors: "Ahmed" vs "Ahmet" — these are plausible responses
- Homophone errors: "no" vs "now" — both are plausible in most contexts
- Confidence is NOT used — this is a semantic check, not a statistical one

## Test Plan
1. English refund call — should pass all plausibility checks (clean STT)
2. Turkish İşbank call — should catch English-garbled transcripts
3. Mixed language call — agent Turkish, caller switches to English mid-call
4. Noisy audio — fragments and partial words

## Latency Impact
- Happy path (PLAUSIBLE): 0ms added to pipeline (check runs async)
- Unhappy path (IMPLAUSIBLE): gpt-4o-mini response time (~100-200ms) + interrupt overhead
- 3s hard timeout: if check is slow, assume plausible and let normal flow continue
