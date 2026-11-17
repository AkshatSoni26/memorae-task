# PRD — Real-Time Google Meet AI Agent

**Project:** Memorae-style Google Meet AI participant  
**Deadline:** Thursday, November 19, 2026  
**Development window:** November 17–18  
**Owner:** Akshat  
**Delivery type:** Working live demo

---

## 1. Product objective

Build an AI agent that can join a Google Meet as a participant, listen to the conversation, recognize when someone addresses it, and respond naturally using voice.

The goal is to demonstrate a working conversational AI participant in a real Google Meet.

The evaluation is primarily about execution speed and whether the agent works reliably in a live meeting.

### Core user experience

1. A user provides a Google Meet URL.
2. The system creates an AI meeting participant.
3. The bot joins the meeting.
4. Participants can speak to the bot.
5. The bot understands when it is being addressed.
6. The bot responds verbally inside the meeting.

**The primary success criterion is that the agent can join, listen, understand, and speak.**

## 2. Scope

### P0 — Must have

- [ ] Create an AI meeting bot using a Google Meet URL.
- [ ] Join the meeting as a visible participant.
- [ ] Receive meeting audio in real time.
- [ ] Detect when someone addresses the agent.
- [ ] Understand the spoken question or instruction.
- [ ] Generate a relevant response using an LLM.
- [ ] Speak the response into the Google Meet.
- [ ] Maintain basic conversational context.
- [ ] Handle a participant speaking while the agent is responding, as far as the chosen provider supports it.
- [ ] Handle meeting admission and joining failures visibly.

### P1 — Only after P0 works

- [ ] Simple web interface to enter a Meet URL.
- [ ] Show bot status: creating, joining, waiting for admission, in meeting, ended, failed.
- [ ] Configurable agent name and system prompt.
- [ ] Basic latency measurements.
- [ ] Graceful handling of disconnects.

### Explicitly out of scope

- Meeting notes and summaries.
- A transcription dashboard.
- Action items and task extraction.
- RAG or document retrieval.
- Tool calling and MCP integrations.
- Calendar integrations.
- User authentication and multi-tenant architecture.
- Persistent conversation memory.
- Complex frontend design.
- Production billing and deployment infrastructure.

Do not implement these during the two-day build.

## 3. Proposed technical architecture

### Primary approach: Hosted Speaking Bots

Use Meeting BaaS Speaking Bots to avoid spending the limited development window building a complete speech pipeline.

**Architecture:**

```text
                 User
                  |
                  v
           Simple Web UI
                  |
                  v
          FastAPI Backend
                  |
                  v
       Meeting BaaS Speaking Bots
                  |
                  v
             Google Meet
                  |
          Live meeting audio
                  |
                  v
       Hosted voice pipeline
                  |
          Speech-to-Text
                  |
                  v
              LLM
                  |
                  v
           Text-to-Speech
                  |
                  v
          Google Meet audio
```

The hosted Speaking Bots API provides the meeting voice pipeline and lets us configure the bot's prompt, voice, and conversation behavior.

### Proposed stack

| Component | Technology |
|---|---|
| Backend | Python + FastAPI |
| Meeting integration | Meeting BaaS Speaking Bots |
| AI model | Provider/model supported by Speaking Bots |
| Speech processing | Managed by Speaking Bots |
| Frontend | Next.js (optional) |
| Deployment | Existing VPS or public HTTPS endpoint |
| Configuration | Environment variables |

### API integration

The Speaking Bots API exposes:

`POST https://speaking.meetingbaas.com/bots`

Example request:

```json
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "bot_name": "Memorae",
  "prompt": "You are Memorae, a helpful AI participant in a Google Meet. Listen carefully and respond naturally when someone addresses you. Keep responses concise and conversational. Do not interrupt participants or dominate the conversation. Do not use tools.",
  "entry_message": "Hey everyone, I'm Memorae. You can ask me anything.",
  "enable_tools": false
}
```

Authentication:

```http
x-meeting-baas-api-key: YOUR_API_KEY
```

The API returns a `bot_id` that can be used to manage the bot.

**Important implementation detail:** Test whether prompt-based addressing reliably prevents the bot from answering every unrelated speaker turn. If it does not, implement an explicit addressing gate using the custom streaming approach.

## 4. Alternative architecture — Custom audio streaming

Use this only if the hosted Speaking Bots approach cannot satisfy the required behavior or customization.

Meeting BaaS supports bidirectional WebSocket audio streaming.

```text
Google Meet
    |
    v
Meeting BaaS Bot
    |
    v
FastAPI WebSocket
    |
    v
Audio / Turn Detection
    |
    v
Speech-to-Text or Realtime Model
    |
    v
LLM Response
    |
    v
Text-to-Speech
    |
    v
Audio WebSocket
    |
    v
Meeting BaaS Bot
    |
    v
Google Meet
```

Important considerations:

- Audio streaming uses raw PCM audio.
- Audio chunks are delivered approximately every 100 ms.
- Supported sample rates include 16 kHz, 24 kHz, 32 kHz, and 48 kHz.
- Both input and output URLs can be configured for bidirectional audio.
- The server must handle JSON handshake messages, speaker events, and binary audio frames.
- Audio should be streamed continuously rather than waiting for a complete response before processing the next incoming chunk.

Use the provider's documented audio format and sample rate consistently.

## 5. Agent behavior and system prompt

The agent should behave like a polite meeting participant.

### Behavior requirements

1. Introduce itself briefly when joining.
2. Listen without interrupting normal conversations.
3. Respond when someone directly addresses it by name or clearly asks it a question.
4. Give short, useful answers.
5. Use the conversation context to answer follow-up questions.
6. Avoid repeating its introduction.
7. Avoid speaking over participants.
8. Never pretend to know information it has not received.

### Initial system prompt

```text
You are Memorae, an AI participant in a Google Meet.

Your role is to participate in conversations naturally
and help participants when they ask you something.

Rules:
- Only respond when someone directly addresses you,
  asks you a question, or clearly invites you to participate.
- Do not interrupt normal human conversations.
- Keep answers short, natural, and conversational.
- Use the context of the ongoing conversation.
- If you do not know something, say so.
- Do not invent facts.
- Do not use tools or claim to perform actions.
- Do not dominate the conversation.
```

**Validation requirement:** Verify the actual silence/no-response behavior in a meeting. A prompt alone is not proof that the voice agent can remain silent reliably.

## 6. Development phases and todos

### Phase 0 — Access and feasibility

**November 17 | Target: 30–60 minutes**

Goal: Confirm that the chosen infrastructure is usable.

- [ ] Create Meeting BaaS account.
- [ ] Obtain API key.
- [ ] Check available credits and usage limits.
- [ ] Read Speaking Bots API documentation.
- [ ] Verify the API endpoint and authentication.
- [ ] Prepare a Google Meet test room.
- [ ] Confirm whether the bot requires host admission.
- [ ] Make one successful bot creation request.

**Exit criteria:** The API accepts the request and returns a bot ID.

Do not spend hours building anything before validating access.

### Phase 1 — Bot joins and speaks

**November 17 | Highest priority**

Goal: Get the first working demo.

- [ ] Create a minimal FastAPI project.
- [ ] Add environment-based API key configuration.
- [ ] Implement `POST /api/meetings`.
- [ ] Accept a Google Meet URL.
- [ ] Call the Speaking Bots API.
- [ ] Return the bot ID.
- [ ] Join a real Google Meet.
- [ ] Admit the bot if required.
- [ ] Verify that the bot appears as a participant.
- [ ] Verify that its entry message is audible.
- [ ] Verify that it can answer a simple question.

**Exit criteria:** A participant asks the agent a question and hears a spoken answer.

This is the most important milestone. If it works, you have the foundation of the demo.

### Phase 2 — Conversational behavior

**November 18 | Morning**

Goal: Make the agent behave correctly.

- [ ] Configure the agent's name and personality.
- [ ] Add the initial system prompt.
- [ ] Test direct addressing by name.
- [ ] Test questions without mentioning its name.
- [ ] Test unrelated human conversations.
- [ ] Verify that it doesn't interrupt unnecessarily.
- [ ] Test follow-up questions.
- [ ] Test short and longer answers.
- [ ] Tune turn-taking and response length.
- [ ] Test whether the bot can handle interruptions.

**Exit criteria:** The agent responds to direct questions and does not constantly interrupt ordinary conversation.

If prompt-based addressing is unreliable, investigate custom audio streaming and add a basic addressing gate.

### Phase 3 — Minimal backend and interface

**November 18 | Afternoon**

Goal: Make the demo easy to operate.

Backend:

- [ ] `POST /api/meetings` — Create a meeting bot.
- [ ] `GET /api/meetings/{bot_id}` — Retrieve bot status.
- [ ] `DELETE /api/meetings/{bot_id}` — Remove the bot.
- [ ] Validate Google Meet URLs.
- [ ] Handle provider errors.
- [ ] Keep API credentials on the server.

Frontend (only if time permits):

- [ ] Google Meet URL input.
- [ ] Join button.
- [ ] Bot status indicator.
- [ ] Bot ID or error display.
- [ ] Leave meeting button.

**Exit criteria:** You can launch and remove the bot without manually calling the provider API.

### Phase 4 — Reliability and demo preparation

**November 18 | Evening**

Goal: Ensure the live demo does not fail.

- [ ] Run at least three complete meeting tests.
- [ ] Test bot admission.
- [ ] Test spoken responses.
- [ ] Test unrelated conversation.
- [ ] Test follow-up questions.
- [ ] Test a failed meeting URL.
- [ ] Test provider/API failure handling.
- [ ] Verify the bot can leave cleanly.
- [ ] Check latency and note observed delays.
- [ ] Prepare a fallback demo meeting.
- [ ] Keep a working API request ready.

**Exit criteria:** Three consecutive successful end-to-end test meetings.

### Phase 5 — Live evaluation

**November 19 | Thursday**

- [ ] Confirm the meeting invitation from Pau.
- [ ] Verify the meeting URL.
- [ ] Check API credits and service availability.
- [ ] Start the agent before the demo.
- [ ] Admit it to the meeting.
- [ ] Introduce the agent.
- [ ] Ask it a direct question.
- [ ] Ask a follow-up question.
- [ ] Have participants continue a normal conversation.
- [ ] Demonstrate that it responds when addressed.
- [ ] Explain the architecture and implementation decisions if asked.

Do not deploy new code immediately before the demo unless a critical bug requires it.

## 7. API design

Keep the backend small.

### Create meeting

`POST /api/meetings`

Request:

```json
{
  "meeting_url": "https://meet.google.com/abc-defg-hij"
}
```

Response:

```json
{
  "bot_id": "provider-generated-id",
  "status": "creating"
}
```

### Get meeting status

`GET /api/meetings/{bot_id}`

Response:

```json
{
  "bot_id": "provider-generated-id",
  "status": "in_call"
}
```

### Remove bot

`DELETE /api/meetings/{bot_id}`

Use the provider's documented bot-removal endpoint.

Possible statuses in our application:

- `creating`
- `joining`
- `waiting_for_admission`
- `in_call`
- `ended`
- `failed`

Map these to actual provider status events and responses. Do not assume every status is available as an exact provider enum.

## 8. Acceptance criteria

The demo is successful if all critical criteria pass.

| ID | Acceptance criterion | Priority |
|---|---|---|
| AC-01 | Bot can be created using a Google Meet URL | P0 |
| AC-02 | Bot joins the actual meeting | P0 |
| AC-03 | Participants can hear the bot's introduction | P0 |
| AC-04 | Bot receives and understands spoken questions | P0 |
| AC-05 | Bot responds with relevant spoken answers | P0 |
| AC-06 | Bot can answer a follow-up question | P0 |
| AC-07 | Bot does not constantly interrupt normal conversation | P0 |
| AC-08 | Bot can be removed from the meeting | P1 |
| AC-09 | Basic status and errors are visible | P1 |
| AC-10 | Demo can be launched without manual API calls | P1 |

## 9. Performance measurement

Do not assume that the challenge requires sub-millisecond end-to-end speech responses unless Pau explicitly confirms that requirement.

A complete speech interaction involves meeting audio transport, speech recognition, model inference, speech generation, and playback. Those are separate latency stages.

Measure:

- End of participant speech.
- Speech recognition completion.
- LLM first-token time.
- LLM response completion.
- TTS first-audio time.
- First audible response in the meeting.

Record observed latency and identify the largest bottleneck.

For the demo, prioritize responsiveness and natural turn-taking over an arbitrary latency number.

## 10. Security and operational requirements

- Store API keys in environment variables.
- Never expose provider credentials in frontend code.
- Validate incoming meeting URLs.
- Do not log API keys or sensitive audio.
- Handle provider API failures and timeouts.
- Use HTTPS/WSS for public endpoints.
- Make participants aware that an AI bot is joining and that meeting audio may be processed.
- Follow the meeting host's admission and consent requirements.

## 11. Risks and mitigation

| Risk | Impact | Mitigation |
|---|---|---|
| Account or API access unavailable | Critical | Resolve access before development |
| Bot cannot join Meet | Critical | Test with a real meeting immediately |
| Bot joins but cannot speak | Critical | Validate entry message before other work |
| Bot answers every speaker turn | High | Test addressing behavior early; use a turn gate if needed |
| Responses are too slow | High | Keep responses short and measure each stage |
| Bot is not admitted | High | Use a test room with host admission available |
| Provider credits exhausted | High | Check quota and usage before repeated tests |
| Internet or provider outage | High | Keep a fallback test plan |

## 12. Definition of done

The project is done when:

1. The agent joins a real Google Meet.
2. It introduces itself.
3. A participant addresses it.
4. It understands the question.
5. It responds verbally in the meeting.
6. It handles at least one follow-up question.
7. It can remain quiet during ordinary conversation.
8. The flow works repeatedly in testing.

---

## 13. Execution rules

1. Build the smallest working version first.
2. Validate every external dependency before building around it.
3. Do not build features outside P0 until the complete voice loop works.
4. Prefer managed infrastructure over custom infrastructure under this deadline.
5. Test in a real Google Meet, not only with local audio files.
6. Keep the code understandable, but do not over-engineer.
7. Save the last few hours for integration testing and the live demo.

**The priority order is: JOIN → LISTEN → UNDERSTAND → RESPOND → SPEAK → RELIABILITY.**

A rough working agent is better than a polished application that cannot participate in a meeting.

## 14. Official technical references

- Meeting BaaS Speaking Bots: https://docs.meetingbaas.com/speaking-bots
- Sending a Speaking Bot: https://docs.meetingbaas.com/speaking-bots/getting-started/sending-a-bot
- Conversation tuning: https://docs.meetingbaas.com/speaking-bots/getting-started/conversation-tuning
- Speaking Bots API reference: https://docs.meetingbaas.com/speaking-bots/reference
- Bidirectional audio streaming: https://docs.meetingbaas.com/api-v2/streaming
- Create a regular meeting bot: https://docs.meetingbaas.com/api-v2/reference/bots/createBot