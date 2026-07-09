# Accessibility posture (product UI)

Done in this build: WCAG AA contrast on all text (#595959 minimum on white),
live-region announcements on attendee and console feeds (role=log,
aria-live=polite) so screen readers announce arriving messages, labeled
controls throughout, visible focus outlines, reduced-motion respected,
dir=auto for right-to-left languages, no color-only meaning (emergency is
color + weight + text).

Before design-partner launch, still needed: a manual screen-reader pass
(VoiceOver + NVDA) of the full join flow by an actual screen reader user,
keyboard-only walkthrough of the console including push-to-talk alternative
(spacebar toggle), and reviews from Deaf SMEs regarding ASL join experience. 
Budget for paid user testing; do not certify from a checklist alone.
