# How to Measure First Turn Latency

## The Only Two Benchmarks That Matter

1. **Latest `EndOfTurn received`** before the agent's TTS starts — this is when the user ACTUALLY stopped speaking (not the first EndOfTurn, which might be a false pause)
2. **`📊 TTS: ttfb=...`** — this is when the agent's audio starts playing

## How to Extract

```bash
python3 -c "
import json
from pathlib import Path

call_dir = list(Path('logs/runs/<RUN_ID>').glob('call_*'))[0]
data = json.loads((call_dir / 'phone_session.json').read_text())

# Find each TTS event (agent speaking) and the latest EndOfTurn before it
tts_events = [(r['t'], r['msg']) for r in data['rawLogs'] if r['msg'].startswith('📊 TTS:')]
eot_events = [r['t'] for r in data['rawLogs'] if 'EndOfTurn received' in r['msg']]

for i, (tts_t, tts_msg) in enumerate(tts_events):
    # Skip greeting (first TTS)
    if i == 0:
        print(f'  t={tts_t:.3f}s  GREETING {tts_msg.strip()}')
        continue
    # Find latest EndOfTurn before this TTS
    prev_eots = [t for t in eot_events if t < tts_t]
    if prev_eots:
        last_eot = prev_eots[-1]
        gap = tts_t - last_eot
        print(f'  EndOfTurn: t={last_eot:.3f}s → TTS: t={tts_t:.3f}s → GAP: {gap:.3f}s')
"
```

## Why This Is Correct

- `EndOfTurn received` comes from Speechmatics when it detects the user finished an utterance
- Multiple EndOfTurns may fire if the user pauses mid-sentence — the LATEST one before the agent responds is the real end of user speech
- `TTS ttfb` is when Cartesia starts producing audio bytes — this is when the caller starts hearing the response
- The gap between these two = EOU processing + LLM TTFT + TTS TTFB = perceived latency

## What's Inside the Gap

```
EndOfTurn → EOU prediction (~200ms) → LLM first token (TTFT) → LLM generates → TTS first byte
```

The LLM TTFT is logged separately as `📊 LLM: ttft=Xms` and is the dominant factor.
With a small prompt (~800 tokens): TTFT ~500-700ms
With the İşbank prompt (~14K tokens): TTFT ~1500ms
