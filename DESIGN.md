---
version: alpha
name: Circuit Relay
description: A quiet instrument panel for one inference engine on a private LAN.
colors:
  background: "#101419"
  surface: "#171e25"
  text: "#e1e6ed"
  muted: "#a5b2bf"
  border: "#3c4752"
  accent: "#a2ddb9"
  danger: "#f1b2a9"
typography:
  sans:
    fontFamily: "system-ui, sans-serif"
  mono:
    fontFamily: "ui-monospace, monospace"
rounded:
  control: "6px"
  card: "10px"
spacing:
  page-max: "1260px"
  card: "23px"
  grid-gap: "18px"
components:
  button:
    minHeight: "40px"
  card:
    borderWidth: "1px"
  dialog:
    maxWidth: "760px"
---

# Circuit Relay Design System

## Overview

### Creative North Star

A local compute control plane, with the restraint of a rack instrument panel: cool charcoal surfaces, pale relay-green action indicators, legible process data and explicit lifecycle stages. Preserve the established dashboard identity. The mint circuit punctuation and numbered engine/load/infer sequence are the signature; registration and authentication use quieter existing surfaces.

### Product context and register

Single administrator, English private-LAN inference tool. A desktop browser is the primary usage scene, with phone-width access supported. Product register throughout. No Japan-market, Japanese-locale, or Japanese-content requirements are established. The task brief and `gateway/app.py` define domain constraints; this file records visual consequences only. Avoid promotional heroes, unrelated branding, ornamental gradients and animated telemetry transitions.

### Token ownership/runtime mapping

`gateway/static/style.css` is the canonical runtime token source. This file mirrors its established values; it is not a second generated theme. The `:root` variables map surface/background/border/accent/text/muted/danger directly to the palette above. Existing compatible legacy selector declarations remain in that same stylesheet. Change the CSS and this record together for durable design changes.

## Colors

Charcoal background and card surfaces separate information with one-pixel borders. Mint identifies enabled actions and visible keyboard focus. Pale red marks confirmed interruptions; errors also use text, never color alone. Secondary text was raised to the muted token for legibility. OS-owned native select popups follow the user platform. Dark is the supported theme; forced-colors preserves system rendering.

## Typography

System sans at 14px for body and controls. Restrained 38px title (28px narrow); 14px section headings; 12px supporting text. Monospace belongs to process identifiers, host paths, API endpoint and metadata. Longer paths wrap rather than disappear behind ellipses. No font downloads.

## Layout

1260px centered main area with 42px/32px outer padding, 18px card gaps and 23px card padding. Three desktop columns, two below 900px, one below 600px. Cards span two columns where lifecycle or registration needs space. Document scrolling owns long forms; only the directory list scrolls inside a bounded dialog. The phone layout preserves every action. Feedback reserves space to reduce shifting.

## Elevation & Depth

Use surface tone and border, not decorative shadows. Modal backdrop dims the same dashboard while the native dialog isolates focus. Dialog width is at most 620px for confirmation or 760px for browsing and stays within the viewport.

## Shapes

10px cards/dialogs; 6px controls; restrained 5px badges. Existing numbered lifecycle strip represents the real sequence. No new icon system.

## Components

Buttons use native semantics, mint primary or transparent secondary surfaces, visible focus and actual disabled attributes. Destructive acceptance uses pale red; the initial focus goes to Cancel. Inputs remain masked until revealed. Native selects retain the platform popup and keyboard model; no popup geometry is authored. Adjustable context offers presets plus a validated custom integer field bounded by the API limits. Session rows and file rows use text plus named buttons. Shared dialogs, notices and API handling are owned by `app.js`.

## Iconography

Native disclosure arrows and select indicators carry their platform meaning. File/folder actions use explicit text rather than a decorative icon set.

## Motion

No new animation. Respect reduced motion. Telemetry updates without transition effects. Active button press shifts by one pixel.

## Content

Sentence case, concrete verbs and truthful unknown states. Distinguish selected context capacity from active/native readback. Explain local-server paths, the inspection boundary, fixed capabilities and administrator setup. Adding a model never implies loading it. Engine update controls reuse the same disclosure, form, confirmation and status patterns. Checking/staging never requests a model load; activation names the authorized test selection. Public diagnostic drafts are editable and require separate GitHub submission.

## Accessibility

Target WCAG 2.2 AA through semantic forms/labels, native selects/dialogs, visible focus, polite status and explicit alerts. All file/session actions work with keyboard, long content wraps, and narrow screens retain the full workflow. Global scrollbars use the runtime palette with a forced-colors escape.

## Verification

`tests/test_dashboard_browser.py` drives Chromium through real password-cookie login and route-fixture capability, registration, expiry, interruption and narrow-screen states. `tests/test_review_dashboard.py` preserves pending-load cancellation coverage. Screenshots and exact commands are recorded in `.superpowers/sdd/2026-10-20-gateway-controls/task-3-report.md`.
