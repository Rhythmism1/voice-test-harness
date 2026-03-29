# Common Mistakes — Don't Repeat These

## 1. Phone agent on wrong LiveKit project

The phone agent reads `LIVEKIT_URL` from `../phone/.env.local` by default, which points to `test-local-convex-project`. When running E2E tests, the phone agent MUST be started with the `e2e-outbound` credentials:

```bash
LIVEKIT_URL=wss://e2e-outbound-o6f8ohvn.livekit.cloud \
LIVEKIT_API_KEY=APIc7fUiT2azPhr \
LIVEKIT_API_SECRET=kcuIHMzW8MSnGeahWzBFSFgcvR5yfHhFlMExEeJWYJMA \
uv run src/main.py dev
```

If you dispatch to `e2e-outbound` but the agent registered on `test-local-convex-project`, it will register fine but NEVER receive the dispatch. There's no error — it just silently does nothing.

## 2. Dispatching Convex `startTestCall` calls the user's real phone

`startTestCall` from Convex uses the `phoneNumber` argument to make a real phone call. This rings a human's phone. Don't use this for automated testing. Use the test harness which dispatches to a LiveKit room and bridges via SIP to the tester agent.

## 3. `getAgentWithGreeting` Convex function doesn't exist

The phone agent on `feat/consensus-stt` calls `agents/actions:getAgentWithGreeting` when `local_agent_config` is not provided. This function doesn't exist on the current Convex deployment. ALWAYS pass `local_agent_config` in the dispatch metadata to avoid this crash.

## 4. Tester agent's `.env.e2e` must point to `e2e-outbound` (not `e2e-inbound`)

The tester agent receives inbound calls via the LiveKit native number on `e2e-inbound`, but it connects as a worker to whatever project is in `.env.e2e`. The dispatch rule on `e2e-outbound` routes SIP inbound to `voice-tester`, so the tester must be registered there — wait, actually check `e2e_config.json` for the correct mapping before starting anything.

## 5. Always check which project each agent is registered on

After starting an agent, verify with:
```bash
strings /tmp/<logfile> | grep "url.*livekit"
```
This shows which LiveKit project it connected to.
