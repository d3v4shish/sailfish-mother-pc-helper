# Sailfish Mother-PC Helper TODO

## Target Validation
- [x] Confirmed SSH reachability to a Sailfish development target.
- [x] Ingested a fresh v1 discovery payload and normalized it as `usable`.
- [x] Generated SSH, webcam preview, webcam expose, and LLs command handoffs.
- [ ] Repeat discovery, webcam, and LLs validation across reconnect, suspend, and reboot scenarios.

## 0. Tracking Rules
- [x] Keep this file focused on the Linux-side helper and always-on agent.
- [ ] Mark objectives done only after the helper works against the real Sailfish services.
- [x] Keep v1 scoped to one Linux mother PC consuming Sailfish discovery, webcam, and LLs control.
- [x] Keep production expectations visible here: logging, visible config, startup guidance, and user cleanup paths.

## 1. Host Baseline And Installation Contract
- [x] Define the minimum Linux host requirements for the helper, including `ffmpeg`, `v4l2loopback`, `systemd --user`, and SSH tooling.
- [x] Keep installation steps explicit and distribution-agnostic where practical.
- [x] Detect missing host prerequisites early instead of failing deep in runtime.
- [x] Avoid hardcoded host paths except for standard user directories or system interfaces.

Acceptance criteria:
- A Linux operator can tell before runtime whether the host is ready.
- Missing prerequisites produce actionable errors.
- Documented install steps do not assume one hidden local setup.

## 2. Always-On Agent Lifecycle
- [x] Provide a user-level service or equivalent always-on process that keeps the latest phone state.
- [x] Start, stop, restart, and inspect the helper without manual process hunting.
- [x] Keep runtime state small enough to recover after reboot or crash.
- [x] Ensure the helper can run unattended without a permanent terminal session.

Acceptance criteria:
- The helper can be managed as a normal user service.
- Rebooting or restarting the host restores the helper predictably.
- The operator can inspect whether the agent is healthy without attaching a debugger.

## 3. Sailfish Discovery Intake And State Canonicalization
- [x] Receive discovery data from the phone and normalize it into one current state record.
- [x] Preserve the latest usable SSH target, webcam URL, LLs control URL, and freshness data.
- [x] Treat stale, partial, or version-mismatched discovery data explicitly.
- [x] Keep the canonical state format simple enough to inspect and script against.

Acceptance criteria:
- The helper always has one obvious latest-known state file or equivalent source of truth.
- The operator can see when discovery data is stale or incomplete.
- The helper does not require manual parsing of logs to find the phone again.

## 4. Reachability And SSH Handoff
- [x] Provide one obvious command or action that uses the latest discovery state to SSH into the phone.
- [x] Detect when the last known address is stale and report that honestly.
- [x] Keep the SSH handoff explicit rather than inventing a large session-management layer.
- [x] Record enough state to explain why a handoff failed.

Acceptance criteria:
- The operator can run one helper action to attempt SSH to the latest known phone target.
- A failed handoff explains whether discovery, network, or SSH itself was the blocker.
- The helper does not hide the actual host and port it is trying to use.

## 5. Webcam Ingestion Contract
- [x] Provide one explicit flow that turns the Sailfish webcam stream into a local `/dev/video*` device.
- [x] Detect whether `v4l2loopback` is present and whether a target video device is usable.
- [x] Keep the ingest path explicit around `ffmpeg` so failures are debuggable.
- [x] Separate live preview from webcam-device exposure so either can be tested alone.

Acceptance criteria:
- The operator can preview the Sailfish stream directly from the helper.
- The operator can expose the stream as a webcam device for Linux desktop apps.
- Missing kernel or userland prerequisites produce actionable errors.

## 6. Operator UX And Control Surface
- [x] Provide one obvious CLI or small local UI for status, SSH handoff, webcam ingestion, and LLs control launch points.
- [x] Add a startup tutorial or first-use guidance that explains the mother-PC role.
- [x] Keep commands explicit instead of burying behavior behind many wrappers.
- [x] Make current known phone state visible from the helper surface.

Acceptance criteria:
- A new operator can discover the main commands quickly.
- The helper exposes current phone identity and endpoint status without editing files by hand.
- Common operations do not require reading source code first.

## 7. Configuration, Pairing, And Trust Boundaries
- [x] Persist helper config in a known user-visible location.
- [x] Keep pairing or trust state explicit and resettable.
- [x] Avoid silent trust of random discovery payloads.
- [x] Make config and state locations visible from the helper or docs.

Acceptance criteria:
- The operator can find config and pairing state on disk.
- Trust can be reset without deleting the whole project.
- The helper distinguishes trusted from untrusted phone announcements.

## 8. Logging, State Files, And Cleanup
- [x] Write logs and state files to standard user-writable locations.
- [x] Make log location, state location, and cache size visible to the operator.
- [x] Let the operator clear helper-created caches or stale state intentionally.
- [x] Keep file creation bounded and documented.

Acceptance criteria:
- The operator can inspect and clean up helper-generated artifacts without guesswork.
- Logs are useful enough to diagnose discovery, SSH, and webcam failures.
- Cleanup does not require manual file hunting.

## 9. Validation And Failure Recovery
- [ ] Validate discovery intake, SSH handoff, webcam exposure, and LLs control launch against a real Sailfish phone.
- [ ] Handle phone reboot, Wi-Fi change, helper restart, and stale-state recovery explicitly.
- [ ] Keep failure messages specific enough that the operator knows the next action.
- [ ] Document the recovery path for each major failure mode.

Acceptance criteria:
- The helper can recover from a phone IP change without manual subnet scanning.
- A host restart or helper restart preserves the overall design and can recover current state.
- Recovery docs cover the common failures a user will actually hit.

## 10. Final Acceptance
- [ ] The helper can track the latest Sailfish phone state and expose it clearly.
- [ ] The helper provides an obvious SSH handoff path to the phone.
- [ ] The helper can turn the Sailfish webcam stream into a usable Linux webcam device.
- [ ] The helper surfaces config, logs, and cleanup paths suitable for ongoing use.
