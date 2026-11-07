# Wi-Fi connectivity architecture and implementation plan

Implement in the existing `feat/wifi-connectivity` checkout. The user supplied the
requirements and explicitly requested autonomous implementation, without a design approval pause.

## Architecture

Keep the existing editor, layout model, conversions, widget/web-clip/gap memory,
App Library handling and apply authorization gates. Add `homescreen/transport.py`
below `PhoneSource`; all three entry points continue to use `Session`.

USB remains the default. Wi-Fi uses USB-bootstrapped RemotePairing and an in-process
pymobiledevice3 userspace tunnel (iOS/iPadOS 17+). Auto prefers USB for the selected
UDID, then Wi-Fi for that same UDID. Ambiguous selection is an error. Connections
are scoped to operations and serialized because pymobiledevice3 supports only one
userspace tunnel per process. No new dependency is needed; require the inspected
11.50.0 API baseline. Pairing records remain owned by pymobiledevice3.

Alternatives: classic network lockdown has known service failures on current iOS;
root-managed tunnels add installation and privilege requirements. Neither fits as
well as Rejig's userspace approach. A separate wireless editor would duplicate the
existing safety and layout code.

Rejig's `patches/wifi-reconnect.patch` pins recovery to the original UDID;
`patches/device-identity.patch` rejects unknown or different identities before
Apply/Restore. Its engine is ios-homescreen's `transport.py` at
97649547dbc44295e451c6bf130a87404481e10c. Consulted these MIT sources and current
pymobiledevice3 source/docs; retain attribution for the adapted provider adapter.
Unlike Rejig's rollback flow, this task requires no automatic write recovery.

## Steps and verification

1. Add transport tests with a fake backend: USB/Wi-Fi discovery, strict selection,
   Auto fallback to the same device, pairing USB-only, timeouts, identity mismatch,
   closure/cancellation. Implement `PhoneConnection.open()` and `discover_devices()`.
2. Route `PhoneSource` operations through that connection, retaining payload logic.
   Test the real adapter with mocked library boundaries and service providers.
3. Add Session tests for wrong/unknown identity, per-device backup identity,
   interrupted writes and failed verification. Save a pending-write marker before
   dispatch, block further writes until an explicit read, never retry a write,
   and return backup plus an explicit uncertain result when readback fails.
4. Add shared configuration for transport/device selection, CLI `devices`,
   `pair-wifi`, `connect`, `reconnect`, flags, and matching MCP tools. Preserve
   existing command names and write gates. Test fixture paths and compatibility.
5. Extend the current device UI with USB/Wi-Fi/Auto, Pair for Wi-Fi, Reconnect,
   transport/status and errors. Preserve the active device when discovery loses it.
   Test protected HTTP endpoints, serialized selection and draft preservation.
   Add local-network/Bonjour declarations to the Mac bundle.
6. Update README, attribution and known limitations. Capture fixture-only UI
   screenshots. Run the full pytest suite, Python/JS syntax and Swift shell build.
7. Request independent review of the whole change, resolve substantive findings,
   inspect/stage only task changes, commit, push to origin and verify remote SHA.
   Open a draft PR against kats1123/homescreen-mcp:main with validation limits.

## Review focus

- Losing USB must never choose a different paired phone.
- Discovery, switching and reconnect must never apply or restore.
- A lost write response must not cause another write or automatic rollback.
- Persistent recovery guard must survive CLI/process restart.
- Widget, folder, App Library and gap payloads must use the existing conversions.

No real iPhone/iPad pairing, settings changes or layout writes are authorized for
this implementation session. All device interactions in validation use mocks or fixtures.

## Execution record

- Baseline: 29 tests passed before edits.
- Transport, PhoneSource, Session and interface regressions were observed failing
  before their implementations, then passed. Current suite: 66 passing tests,
  including an MCP SDK stdio client/server fixture round trip.
- Browser QA used only a simulated PhoneSource/fixture. Verified transport
  selection, reconnect, pairing confirmation cancellation, page insert/Undo and
  unchanged draft; no console warnings/errors. Screenshots are explicitly simulated.
- Independent whole-change review found three P2 issues. Fixed UI CLI-mode
  initialization and discovery cancellation with RED→GREEN regressions; added
  attribution to wheel license-files and Mac bundle resources, inspected artifacts.
- Swift release build and Python/JS/shell syntax checks passed. Full frozen engine
  and Mac bundle checks are recorded in the PR with remaining hardware validation.
- Ruling: connections are operation-scoped, without remembered IP fallback. This
  limits persistent socket failures and global userspace stack conflicts; Bonjour
  must work on the network, and operations have connection-setup overhead.
- Ruling: preserve legacy backups in their original per-device directory for
  compatibility; new backups embed identity. A legacy backup manually copied
  between device directories cannot prove its provenance, so documentation warns
  against doing that.
