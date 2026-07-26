# Buzz + Language Layer

Post once in your team's Buzz workspace; everyone in a Language Layer space
receives it on their own device in their own language and format.

Buzz gives a team one room. Language Layer makes the room understood.

## Recommended path: Buzz Workflows

Buzz (github.com/block/buzz) ships a Workflows feature (Experiments >
Workflows) that can forward channel messages to any public HTTPS endpoint.

1. Create a Language Layer space at langlayer.onrender.com/host and note its
   access code (LL-XXXX).
2. Get the channel id: open langlayer.onrender.com/v1/events/LL-XXXX and copy
   the `channel_id` value (chn_...).
3. In Buzz: Experiments > enable Workflows, then create a workflow on the
   channel you post announcements in. Edit as YAML:

```yaml
name: Language Layer announcements
description: Forward announcements to Language Layer for multilingual delivery
trigger:
  on: message_posted
steps:
  - id: forward
    action: call_webhook
    url: https://langlayer.onrender.com/v1/channels/CHANNEL_ID/events
    method: POST
    headers:
      Content-Type: application/json
    body: '{"channel_id":"CHANNEL_ID","priority_class":"announcement","kind":"text","payload":"{{trigger.text}}"}'
```

4. Join attendees to the space (scan the QR or enter the code), post in the
   Buzz channel, and every attendee receives it in their language.

Status note (July 2026): on Buzz's hosted communities, workflow definitions
save and activate, but webhook execution did not fire in testing, and
workflow runs report empty. Filed with the Buzz team. On a self-hosted relay
built with webhook support enabled, the same workflow should execute.

## Protocol-level alternative: direct Nostr bridge

`buzz_nostr_bridge.py` connects to a Buzz community relay as an external
Nostr client: NIP-42 authentication, kind-9 channel messages, `#h` channel
filtering. Findings from testing against a hosted community: authentication
and history reads work; live event fan-out to external subscribers is gated
by relay-side channel membership, so live forwarding requires either
membership registration or a self-hosted relay. The bridge is included as a
working reference for the protocol path.

## The demo

One screen: post an announcement in Buzz. Side by side: three attendee
devices showing the same message in Spanish, Mandarin, and Portuguese, each
labeled, each with a delivery receipt.
