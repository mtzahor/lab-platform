# Web accessibility

Phase 7 targets WCAG 2.2 AA behavior for its core operating journey. Accessibility is a release
property, not a theme: new pages must preserve keyboard access, semantics, visible focus, readable
status, and reduced-motion behavior.

## Implemented conventions

- A skip link moves focus to the main content region.
- The shell uses labelled navigation, header, main, aside, table, form, and dialog landmarks.
- Every input has a persistent label; required and error states are expressed in text.
- Buttons have accessible names even when visually icon-only.
- Dialogs expose a title, keep an explicit close action, support Escape, and return focus to the
  initiating control.
- Loading and live-connection changes use polite status regions. Errors use readable text and do
  not rely on a toast alone.
- Status badges combine words/icons with colour. Unknown, degraded, stale, denied, and terminal
  states remain distinguishable without colour perception.
- Data tables retain real headers and rows, have keyboard-reachable controls, and become scrollable
  rather than collapsing labels away.
- Serial output stays selectable text with a labelled viewer and does not inject terminal control
  sequences as markup.
- CSS honors `prefers-reduced-motion`; no workflow meaning depends on animation.
- Responsive layouts keep actions reachable at narrow widths and do not require horizontal pointer
  precision for ordinary forms.

## Keyboard acceptance pass

For every core release, complete this pass with only a keyboard:

1. Skip to content, open/close mobile and desktop navigation, and identify current location.
2. Sign in and sign out without a pointer.
3. Search and filter benches; open a row and return without losing context.
4. Open, validate, submit, and cancel reservation, queue, flash, and workflow dialogs.
5. Follow workflow progress, pause/follow serial text, and reach artifact downloads.
6. Use every destructive confirmation; verify Escape/cancel cannot trigger the action.
7. Create a user and role assignment in administration.
8. Filter the audit table and open a detail view.
9. Trigger a validation error, server denial, network error, and session expiry; ensure focus and
   message order make recovery clear.

Focus must remain visible throughout. A route change should move the reading context to the main
heading or content region rather than leave focus on a removed control.

## Screen-reader acceptance pass

Check at least VoiceOver with Safari on macOS and one Chromium-based screen reader combination.
Verify:

- page and dialog headings describe the current resource;
- organisation, principal, live-state, progress, and error messages are announced once;
- tables announce header relationships and action buttons identify their row resource;
- required fields and inline errors are associated programmatically;
- disabled versus permission-absent actions are understandable from surrounding text;
- `UNKNOWN` and `RECONCILING` are read as words with their explanatory message;
- serial line numbers do not overwhelm ordinary reading navigation.

## Contrast, zoom, motion, and scale

Test light/dark system preferences where supported, 200% browser zoom, and a 320 CSS-pixel
viewport. Text and interactive controls must meet AA contrast. Focus rings and error outlines need
non-colour cues. At 1,000 bench rows, pagination/virtualization must not remove accessible row
names or make focus jump as rows are recycled.

## Automated and component checks

Testing Library queries should prefer roles, labels, names, and visible text. Add an automated
accessibility scanner to the browser suite when its runtime is available, but do not treat a clean
scan as a substitute for the keyboard/screen-reader passes above. Test permission-aware rendering,
dialog labelling, validation association, live status, empty states, and destructive confirmation
as components.

Record any known exception with the affected route, assistive technology, impact, workaround,
owner, and target release. Do not silently waive a core cut-line flow.
