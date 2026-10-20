# Live acceptance — 20 October 2026

Implementation under test: `4990e06f3a637ff05c611db065d75abe79eeb785`.
The follow-up changes the Gufo example's native matcher, documentation and regression
tests; the packaged gateway runtime is unchanged.

## Setup and scope

The operator stopped the existing system service during an idle maintenance window.
A privately staged candidate runs on the existing LAN address using a cloned Python
3.14.8 environment, the built wheel with MCP dependencies, separate state and temporary
test credentials. Existing harness credentials cannot control the candidate. The original
deployment environment, configuration, credentials and system unit remain backed up and
unchanged. The original service remains stopped while the candidate owns its port.

Existing model files were reused without modifying weights. Engine launch recipes kept
their unrelated cache, slot and memory arguments. Context bindings and private generated
Strata configuration supplied the tested capacity. Tests used short prompts and bounded
outputs; these are functional smoke checks, not long-running stability or maximum-context
quality measurements. Private evidence and backups are excluded from Git.

## Results

| Area | Observed result |
|---|---|
| llama.cpp / Qwen Coder | 65,536 and 131,072 capacities read from native slots; context resize replaced the process; repeated load with omitted settings preserved process, settings and selection ID. |
| Request admission | Streaming completed; a bounded active generation blocked ordinary unload and resize with 409; stale selection ID was refused after resize. |
| Qwen reasoning on llama.cpp | Off produced no reasoning output; On produced reasoning; an explicit request disabled it. Default changes preserved the native process. |
| Strata / Flash Next | Loaded at 65,536 with native slot readback, completed a short request, and unloaded both native processes. |
| Kyojin / Flash Next | Loaded at 65,536 with native slot readback, completed a short request, and unloaded its native process. |
| DwarfStar / DeepSeek V4 Flash | Loaded at declared 65,536, completed a short request, and unloaded its native process. This gateway-only profile has no native context readback. |
| Gufo 0.19.0 | Registered the existing Qwen 27B GGUF; loaded at native-reported 65,536; text, SSE, reasoning defaults/override, Responses, Messages and client-managed function-tool round trips passed. |
| Gufo occupancy and shutdown | A direct native request was visible as busy with zero gateway-tracked requests; normal unload returned 409. Subsequent idle unload removed the tracked native process and temporary container. |
| Browser | Actual Chromium login, context/reasoning controls, HttpOnly/SameSite=Strict remembered cookie, restoration after closing/reopening the browser, and logout passed. HTTP sessions were explicitly enabled for this LAN test. |
| Browser/API authentication | Wrong password, missing CSRF and wrong bearer role were refused. Remembered session survived candidate restart; individual session revocation and logout removed access. Browser cookies could not authorize inference. |
| Registration | Actual GGUF inspection/addition, duplicate refusal, restart persistence and removal passed. A deleted disposable artifact disabled only its registration. Removing a registration kept actual weights intact; removing a loaded registration was refused. |
| MCP | Packaged Python 3.14 stdio adapter exposed six tools, explicitly loaded a real model and completed bounded delegation through the candidate API. Invalid load/delegation did not implicitly load a model. No force or URL tool arguments were exposed. |
| Installed OpenCode 1.18.30 | Generated 65,536 client configuration completed a short task while backend capacity remained 131,072 with unchanged process/selection. Disposable configuration and tool permissions were used. |
| Installed Hermes | Generated 65,536 client configuration completed a short task while backend capacity remained 131,072 with unchanged process/selection. An isolated data root preserved existing profiles. |
| Regression suite | 194 tests passed on Python 3.12 after the matcher correction; Ruff lint/format and whitespace checks passed. |

Gufo used upstream's official runtime image, pinned to
`ghcr.io/gufo-org/toolboxes/gufo-runtime@sha256:51be93845170ce3ce441c7399b5b2eba942baf7f3e6c9a4a3333fccecc27d190`.
It reports `0.19.0 (7b27845)`, matching the reviewed upstream source revision. The
temporary rootless container bound the engine to loopback, forwarded
`IGM_ENGINE_RUN_ID`, and mounted existing weights read-only. No host packages or
persistent SELinux settings were changed.

## Finding corrected during acceptance

The Nix-built executable in the official Gufo container is `.gufo-wrapped`, although
its command line begins `/bin/gufo`. The example matched only ordinary executable
names, so the initial ready server was absent from native process enumeration.
The example now includes the exact wrapped executable name. A regression test first
failed for that executable, then passed after the correction. The real candidate was
restarted with the additional matcher; ownership, native occupancy and full native exit
were then verified successfully. No broad process-name matching was added.

The first isolated Hermes invocation also used an unsupported `none` toolset name and
exited before inference. Correcting only the test invocation to its installed valid
`context_engine` toolset produced a successful completion; no gateway fix was needed.

## Remaining acceptance and deployment gates

- Operator verifies remembered login in their own normal browser with a password they
  set privately. Automated Chromium checks have passed.
- A physically separate LAN host installs the wheel with the MCP extra and exercises
  discovery, explicit load, bounded delegation and unload in its actual coding harness.
  Local stdio-to-LAN-HTTP checks do not establish remote-host routing/firewall behavior.
- The operator runs a small representative Hermes/OpenCode coding task from that host,
  checking streaming/tool behavior and its chosen conversation budget.
- CI passes again on the follow-up commit. Earlier green CI covered the original PR head.
- Choose and validate final service deployment or rollback before resuming ordinary
  workloads. Starting the original service requires the candidate to release its port;
  the original service was already in an unloaded backend-error state before this window.

The PR is not merged. Do not treat these smoke checks as a claim of long-duration
engine stability, arbitrary model compatibility, or completed production deployment.
