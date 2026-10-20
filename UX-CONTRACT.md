# Circuit Relay UX contract

## Scope and evidence

English single-admin LAN control dashboard. The authoritative domain evidence is `gateway/app.py`, `gateway/browser_auth.py`, `gateway/registry.py` and `.superpowers/sdd/2026-10-20-gateway-controls/task-3-brief.md`. Password cookies grant control access; control and inference credentials remain separate. Model inspection/registration and lifecycle endpoints enforce their own authorization and file confinement. No billing or regulated deletion workflow exists here.

## Canonical UI map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | Native HTML select in index.html | This contract; DESIGN.md | OS-owned popup | Chromium keyboard/open state |
| Form | Native labels/forms and app.js submit handlers | Domain endpoints | Password, key, registration | Browser workflow tests |
| Scrollbar | Global style.css baseline | DESIGN.md | Root and directory/dialog scrolling | Computed style/browser |
| Toast | Persistent notice/status/alert regions | This contract | Global notice, scoped form feedback | Browser workflow tests |
| CRUD | app.js inspect/review/add workflow | registry.py and API | Register and revoke session | Full browser flow |
| Dialog | Shared native dialog helper in app.js | This contract | Force, recovery, revocation; file browser | Escape, initial Cancel focus, focus return |

Table selection and date entry are not applicable. Native select popup geometry and platform keyboard behavior are intentional. Browser dialogs such as window.confirm are forbidden; native HTML dialog is the app-owned modal primitive.

## Flow ledger

| Operation | Pending and success | Failure and recovery | Policy source |
|---|---|---|---|
| Password sign-in | Disable submit; clear password; connect live data; optional trusted-browser persistence | Inline error, focus password; no stored client secrets | browser_auth.py; auth endpoints |
| Restore/sign-out/expiry | Restore HttpOnly cookie on load; sign-out clears controls/data; expiry stops polling and requires reauthentication | Network failure retains the session and offers retry; failed server logout does not imply revocation | browser_auth.py; auth endpoints |
| Revoke browser | Confirm named browser and consequence; DELETE; refresh list or disconnect current browser | Keep list/action available for retry | auth session endpoints |
| Engine/model lifecycle | Select engine before load; show supported presets/custom launch capacity and active settings separately; busy action gate; explicit Refresh models preserves supported selections | Show backend error; preserve selection; intentional force can cancel a pending load | model and engine endpoints |
| Recovery | Named app-owned confirmation, Cancel initially focused; pending state; retry telemetry on return | Show scheduling error and restore controls | recovery endpoints |
| Browse | Approved roots; path and Up; local folder filtering with Clear; first 100 then Show more | Empty/no-results and root setup guidance; errors leave close/root selection usable | model-files endpoint; registry.py |
| Inspect/add | Select profile and server paths; inspect; show metadata/warnings; candidate saved verbatim; refresh catalog and select without load | Every input change invalidates candidate; stale responses ignored; error requires inspection before retry | models endpoints; registry.py |

## State and async behavior

Auth generation prevents old protected responses from repopulating a disconnected UI. Polls do not overlap. Context initially restores the matching loaded engine/model requested launch capacity, including custom values; only bounded integers are accepted, with valid active/catalog/minimum fallbacks. Reasoning initially uses the active default for a matching loaded engine/model, otherwise the saved catalog default; background polling and explicit catalog refresh preserve subsequent user edits. Folder requests use a sequence so slow responses cannot overwrite a new root/folder. Inspection has a revision and auth generation; any changed path, profile, ID, context or reasoning invalidates its save candidate. Local directory filter is transient and not stored in URL because it refers to private server paths within a modal. Password/key/token are never put in storage or URLs.

Cookie mutations include CSRF and credentials same-origin. Authorization is present only when a control key is nonempty. A CSRF/session failure clears browser controls and requires sign-in; unrelated forbidden errors remain actionable API feedback. Unknown/native telemetry never renders as zero utilization except meter backgrounds.

## Accessibility and responsive behavior

Semantic labels, buttons and native selects are the baseline. Modal naming, focus isolation, Escape and background inertness come from HTML dialog. Serious dialogs initially focus Cancel and return to their trigger when closed. Forms own validation, field association and error focus. Password reveal has an explicit changing accessible name. Status/alerts provide scope without repeated toast overlays. Folder entries and session labels render as text, never markup.

Single-column phone layout preserves actions and lets long server paths wrap. No fixed-height ancestor traps registration fields. Reduced motion and forced-colors retain usable controls. The document title identifies the single dashboard route.
