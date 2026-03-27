# Voice Agent Test Harness

Autonomous testing framework for voice agent development. Runs agent-to-agent conversations in LiveKit rooms, captures structured logs from both sides, and computes metrics.

**This is a Claude-first repository.** The primary user is an AI coding agent (Claude Code) operating in an iterative test loop: make a change to the phone agent → run a scenario → read the metrics → decide if the change worked → repeat.

## Problem Statement

Voice agent development has a slow feedback loop: change code → deploy → manually call → listen → judge quality. This makes empirical testing of STT accuracy, latency, prompt changes, and multi-language support impractical at scale.

This harness creates a **programmatic feedback loop**: define a test scenario (prompts, language, expected outcomes), run it, get metrics back — all without a human in the loop.

## Architecture

```
┌─────────────────┐     LiveKit Room      ┌──────────────────┐
│   Test Agent     │◄────────────────────►│   Phone Agent     │
│   (tester/)      │     Audio + Data      │   (../phone/)     │
│                  │                       │                   │
│  - Reads script  │                       │  - Real agent     │
│  - Speaks prompts│                       │  - Ensemble STT   │
│  - Records STT   │                       │  - Session logs   │
│  - Logs results  │                       │  - Metrics        │
└────────┬─────────┘                       └────────┬──────────┘
         │                                          │
         ▼                                          ▼
   logs/runs/<id>/                       ../phone/logs/sessions/
   tester.json                              <timestamp>.json
         │                                          │
         └──────────────┬───────────────────────────┘
                        ▼
                  analyze.py
                  (WER, latency, STT accuracy, turn counts)
```

## Paths & Dependencies

This harness knows about the phone agent's location relative to itself:

| What | Path | Purpose |
|------|------|---------|
| Phone agent source | `../phone/` | Started as subprocess via `uv run src/main.py dev` |
| Phone session logs | `../phone/logs/sessions/*.json` | Structured per-call events (STT turns, LLM/TTS metrics, ensemble validations, raw logs) |
| Phone env | `../phone/.env.local` | Shared LiveKit + provider credentials |
| Test run logs | `./logs/runs/<run_id>/` | Tester results + copied phone session log + analysis |

## What Claude Needs Access To

This repo is designed to be operated by Claude Code. Here's what it needs:

### Tools / Access
- **Filesystem (read/write)** — read scenario YAML, read session logs from `../phone/logs/sessions/`, write analysis results
- **Bash/shell** — start agent subprocesses (`uv run`), manage LiveKit rooms
- **LiveKit Server API** — create rooms, generate tokens, delete rooms (via `livekit` Python SDK, authenticated by env vars)

### No External Services Needed
- **No Convex access** — all data comes from local session log files written by the phone agent in dev mode
- **No MCP servers** — everything runs through shell commands and Python SDK calls
- **No browser** — fully headless, CLI-only operation

### Environment Variables (from `../phone/.env.local`)
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` — room management + agent connection
- `CARTESIA_API_KEY` — TTS for the test agent's voice
- `DEEPGRAM_API_KEY` — STT for recording what the phone agent says back
- `OPENAI_API_KEY` — optional, if tester needs LLM for dynamic prompts

## Components

### 1. `run.py` — Orchestrator
Creates a LiveKit room, dispatches both agents with metadata, waits for completion, copies phone session logs, triggers analysis. This is the entry point for all test runs.

### 2. `tester/` — Test Agent
A lightweight LiveKit agent that:
- Joins a room as the "caller"
- Speaks scripted prompts via TTS (configurable: language, provider)
- Listens to agent responses via STT
- Logs everything: what it said, what it heard, timestamps
- Hangs up after the script completes

### 3. `scenarios/` — Test Scenarios
YAML files defining test scripts:
```yaml
name: navy_seal_passage
language: en
prompts:
  - text: "One of the traits that set Navy Seal teams apart..."
    wait_for_response: true
  - text: "What can you help me with?"
    wait_for_response: true
expected_keywords: ["navy", "seal", "stealth", "adaptability"]
thresholds:
  max_avg_llm_ttft_ms: 1500
  min_ensemble_word_confidence: 0.8
  max_wer: 0.15
```

### 4. `analyze.py` — Post-Run Analysis
Reads logs from both agents, computes:
- **WER** (Word Error Rate) — compare tester's intended text vs phone agent's STT output
- **Latency** — LLM TTFT, TTS TTFB, EOU delay from phone agent session logs
- **STT Accuracy** — ensemble word confidence scores from session logs
- **Turn Count** — did the conversation flow correctly?

Can be called standalone or by the orchestrator:
```bash
uv run python analyze.py logs/runs/<run_id>/
uv run python analyze.py logs/runs/<run_id>/ --json  # Machine-readable
```

### 5. `scripts/` — Utility Scripts
- `start_phone_agent.sh` — starts the phone agent in dev mode
- `run_scenario.sh` — runs a single scenario end-to-end
- `batch_run.sh` — runs multiple scenarios sequentially

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Copy env from phone agent (shares LiveKit credentials)
cp ../phone/.env.local .env.local

# 3. Run a test scenario
uv run python run.py scenarios/basic_english.yaml

# 4. Analyze results (auto-runs after step 3, or standalone)
uv run python analyze.py logs/runs/<run_id>/
```

## The Iterative Loop (How Claude Uses This)

```
Human: "Check WER on Turkish STT"

Claude:
  1. Reads scenarios/turkish_stt.yaml
  2. Runs: uv run python run.py scenarios/turkish_stt.yaml
  3. Reads: logs/runs/<id>/analysis.json
  4. Reports: "WER is 28%, above 25% threshold. Validator v[3] missed 'Istanbul'."
  5. Makes code change to phone agent STT config
  6. Re-runs scenario
  7. Reports: "WER dropped to 19%. PASS."
```

## Use Cases

1. **STT Accuracy Testing**: Run the same passage through different STT providers, compare WER
2. **Language Testing**: Spin up Turkish/Arabic test agents, measure STT accuracy per language
3. **Latency Regression**: Run baseline scenarios after code changes, compare p50/p90 latency
4. **Ensemble Validation**: Verify ensemble consensus scores with different validator counts
5. **Prompt Testing**: Change agent prompts, run conversations, check response quality

## Expanding This

1. **CI Integration**: Run `batch_run.sh` in GitHub Actions on PR, fail if WER exceeds threshold or latency regresses beyond baseline
2. **A/B Scenario Matrix**: Define a matrix of (STT provider × language × prompt) and run all combinations, output a comparison table
3. **Pre-recorded Audio**: Replace TTS with real recorded audio files to eliminate synthetic speech bias — most impactful improvement for accuracy measurement
4. **Adversarial Testing**: Add scenarios with background noise, accented speech, interruptions, silence gaps — stress-test edge cases

## Considerations & Risks

### What Could Go Wrong
- **Monotony bias**: AI test agents speak with perfect cadence and zero hesitation. Real callers stutter, pause, speak over the agent. Test results will be optimistic compared to production. Mitigation: add noise injection, variable pacing, and eventually pre-recorded real audio.
- **TTS-STT feedback loop**: If both agents use the same TTS/STT provider, they're testing the provider's ability to transcribe its own output — not real speech. Mitigation: use different providers for tester TTS vs phone agent STT (e.g., Cartesia TTS → Speechmatics STT).
- **Flaky timing**: LiveKit room creation, agent connection, and audio routing have variable latency. Tests may occasionally fail due to infrastructure timing, not code bugs. Mitigation: add retries and timeout thresholds, distinguish infra failures from test failures.
- **Metric drift**: WER and latency baselines shift as providers update their models. A "regression" might be the provider, not your code. Mitigation: track provider model versions in run metadata, compare against same-version baselines.
- **False confidence**: Passing tests ≠ working product. These tests cover scripted prompts on the happy path. They won't catch edge cases in real conversations. The harness supplements manual testing, it doesn't replace it.
