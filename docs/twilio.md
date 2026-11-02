# Twilio phone calls

Audiva uses Twilio Programmable Voice for clinic incoming/outgoing calls and
Outreach campaigns. Browser WebRTC voice previews use their existing transport.

## Configuration

Set these backend-only values in `.env` or the deployed backend environment:

```dotenv
TWILIO_ACCOUNT_SID=ACyour-account-sid
TWILIO_AUTH_TOKEN=your-account-auth-token
TWILIO_PHONE_NUMBER=+your-twilio-voice-number
PUBLIC_BACKEND_URL=https://your-public-backend.example.com
```

Copy the Account SID and Auth Token from the same Twilio account in Console. Use
an eligible voice-enabled calling number in E.164 format. Keep the existing
`SARVAM_API_KEY`, database and JWT configuration, then restart the backend.
The REST client uses HTTP Basic authentication at
`https://api.twilio.com/2010-04-01/Accounts/{account_sid}/`; no new SDK is needed.

Twilio's current Voice trial blocks the `<Stream>` verb required by this AI
phone integration and restricts call-creation parameters. Verified destination
numbers alone do not enable streaming. An upgraded account is needed for this
phone flow; use Audiva's browser voice preview for development without Twilio
calling charges. See [Twilio's blocked trial verbs](https://www.twilio.com/docs/usage/trials/try-out-voice#blocked-verbs).

## Outgoing calls

Start a clinic call from Phone calls or run an Outreach campaign. Audiva creates
the call with `POST Calls.json`, setting `From`, `To`, the HTTPS TwiML `Url`, and
`StatusCallback` events for initiated, ringing, answered and completed.
Twilio immediately returns the actual call SID, which Audiva persists and polls.

When answered, Twilio requests the answer URL. Audiva returns bidirectional TwiML:

```xml
<Response>
  <Connect><Stream url="wss://your-backend/media/SCOPED_GRANT" /></Connect>
  <Hangup />
</Response>
```

Audio uses 8kHz mono mu-law. Pipecat's Twilio serializer converts audio to/from PCM
for the existing Sarvam pipelines and sends `clear` messages for interruptions.
Closing the stream lets Twilio execute `<Hangup/>`.

Clinic callbacks are under `/api/v1/clinic-phone/twilio/calls/{call_id}/answer` and
`/status`. Its media endpoint is `/media/{token}`. Outreach uses
`/api/v1/outreach/twilio/{call_id}/answer`, `/status`, and `/media/{token}`.
The media grant is in the URL path because Twilio Stream URLs do not support query
strings. Scoped grants are redacted from Uvicorn logs. The public backend must
support HTTPS requests and secure WebSocket upgrades, including through ngrok.

## Incoming clinic calls

A platform operator listed in `CLINIC_OPERATOR_USER_IDS` assigns a Twilio number
in the dashboard. Copy its private **Incoming call webhook (POST)** URL from the
route details. In Twilio Console, open that number's Voice configuration, set
**A call comes in** to **Webhook**, paste the URL and choose **HTTP POST**, then save.
The API returns this URL as `inbound_answer_url`.

The HTTPS webhook returns TwiML. Audiva checks the active Twilio call and called
number, then creates a media grant bound to that call. Saving or disabling the
route invalidates its old URL. Update the Twilio number's webhook after saving.

## Status and recovery

Twilio callbacks use `CallSid` and `CallStatus`. Audiva normalizes Twilio's
`canceled` status to the existing `cancelled` database state. Duplicate and
out-of-order callbacks never reopen terminal calls. Polling uses the same Twilio
Call resource for queued, active and completed calls.

A timeout, server error or malformed call-creation response can still have placed
a real call. Audiva marks that attempt `unknown`, reserves its campaign slot and
does not automatically redial it. Check Twilio's call logs, then link the actual
call SID in the dashboard or explicitly confirm no call was placed. Retry clinic
HTTP submissions with the same request ID to avoid duplicate dials.

Logs under `app.twilio` contain application call IDs, provider SIDs, timings and
sanitized provider errors without credentials or full phone numbers. Historical
call records remain intact. Resolve any active calls on the previous provider
before switching a running deployment; those call IDs cannot be queried in Twilio.

## References

- [Twilio Calls API](https://www.twilio.com/docs/voice/api/call-resource)
- [TwiML Connect Stream](https://www.twilio.com/docs/voice/twiml/stream)
- [Media Streams protocol](https://www.twilio.com/docs/voice/media-streams/websocket-messages)
- [Trial account restrictions](https://www.twilio.com/docs/usage/trials)
