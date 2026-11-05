# Browse performance changes

## Round 2 scope and findings

This gate requests approval for Slice 1 only, the configuration-only title-open improvement. It authorizes no Compose/environment change, cache-TTL rollout, app restart, image pull or database restore. Slice 2 remains parked, not approved, until its unresolved recovery requirement is met. This is an explicit partial delivery, not completion of every audit recommendation.

1. Round-1 single-attempt-write finding: add `retry_429=False` explicitly to both forward and rollback PUT calls in the existing guarded write helpers. Reconcile an uncertain forward or rollback response with GET, not another PUT. Test HTTP 429, transport ambiguity, readback mismatch and failed-rollback reconciliation.
2. Round-1 Slice 2 recovery finding: unresolved and excluded from this Slice 1 approval. Do not implement or apply the Compose/cache slice until exact failed-deployment and completed-transaction rollback has been supplied, rehearsed and reviewed. Its original design gate retains one focused correction opportunity. Narrowing this gate does not reset its budget.

## Outcome and scope

Make native title details faster without changing playback, account identity, household history or media quality. Keep the current AIOStreams image and existing host. Implement reversible configuration before considering an upstream fork or image service.

Owner authorization: Oracle review, then implementation and rollout of the performance fixes. Tracking issue: #16. Baseline stack commit: dc87b95b22af. Runtime: v2.37.0, application commit ed8b085f081b. The application and connector already have immutable image pins.

The audit measured 26 usable home rows and six native personas. A 30-worker warm browse test had no failures and p95 0.842 seconds. The larger gaps were title-detail resolution, repeated full metadata and backdrop bytes. The audit was not a player render trace. Earlier hosted metadata HTTP 429 errors were not reproduced by the later browse test.

## Decisions

1. Trial `jellyfin.resolveOnOpen=false` on the existing primary configuration. This covers its native personas, not the separate Stremio configurations. Keep instance mode `user`. An explicit MediaSources request can still resolve. Do not set instance mode `never` or disable RemuxDB, subtitles, filters or rate limits.
2. Park targeted metadata-cache rollout pending Slice 2 recovery qualification. The proposed one-hour TMDB-addon TTL is not authorized by this gate. Preserve current cache settings.
3. Do not add an image proxy, force relay or lower all backdrop sizes. Existing server resizing depends on the player's requested width, and a full-screen TV hero must not become a 480-pixel image. Keep image sizing and missing-logo handling as explicitly unimplemented client/upstream follow-ups unless an existing supported setting is proved to solve the exact failure. A server-only rollout must not be reported as fixing player rendering or the missing logo.
4. Do not add a shared metadata limiter unless a controlled test reproduces throttling after cache tuning. Do not upgrade to a nightly or self-host another addon for this change.

## Slice 1: safe resolve-on-open control

Add a small CLI at `ops/browse-tuning.py`, reusing `common.py` and existing snapshot/write verification helpers. It operates on `main` only and refuses other targets before HTTP calls. Default invocation is read-only. Provide mutually exclusive `--dry-run` and `--apply`, `--expect-sha256 HASH`, and `--resolve-on-open true|false` (default false) so the same reviewed path can restore the previous setting. Reject malformed hashes before HTTP calls and require an existing explicit boolean preference instead of guessing a missing previous value.

Read the complete current configuration after one instance-admin login. The candidate must be an exact deep copy with only `jellyfin.resolveOnOpen` changed. Refuse a missing or malformed Jellyfin object. Never create a configuration, rebuild presets or reconcile household names. Print only the changed path, old/new boolean, a canonical full-config precondition hash and the snapshot path. Hash canonical configuration including settings outside the tuned field, with only the established server-maintained exclusions from `common.py`. Do not use the existing template-only hash, which omits Jellyfin.

On apply, independently reread and match the supplied precondition before the write. Save an owner-only target-bound snapshot outside the sync tree. Use the existing `guarded_write` and `rollback_config` helpers. Set `retry_429=False` on every forward and rollback PUT. GET reconciliation remains bounded and retryable. Attempt a failed or uncertain PUT once. Reconcile through GET and accept only a verified full candidate. Report verified unchanged state without repeating the write. Retain the snapshot and stop without rollback for unknown or drifting state. This includes unavailable or malformed readback. Permit bounded rollback only after a fresh independent GET proves the exact expected candidate state. Refuse rollback if this precheck fails or finds a later edit. If the rollback PUT itself fails, independently GET the configuration. Accept restoration only if the complete canonical old state matches. Otherwise stop and report the retained emergency snapshot. Never claim restoration from a successful response alone. Verify full canonical readback, including credentials and persona objects. No configuration endpoint provides compare-and-swap: establish a cooperative configuration-writer exclusion window and acknowledge the residual external-writer race between final GET and PUT. If exclusion cannot be established, do not apply.

Change `desired_jellyfin` so normal bootstrap preserves an existing resolveOnOpen boolean instead of resetting it to true. Keep true as the default for new configurations to avoid widening the rollout. Preserve unrelated convergence behavior.

Tests must verify these conditions:

- Dry-run performs no PUT or snapshot. Apply without a matching full precondition refuses. Unrelated-field drift refuses. Malformed/standalone targets refuse.
- Only the intended boolean changes. Credentials/personas/custom keys survive.
- 429 PUT executes once before GET reconciliation. Uncertain forward and rollback PUTs reconcile. Tests distinguish unchanged/committed/unexpected states and verify restoration.
- Repeat apply makes zero changes. Bootstrap preserves true and false.

Wire CLI help and tests into existing CI. No code comments or attribution trailers.

Run the actual CLI executable against an isolated HTTP stub, with fake credentials, before publication. Exercise dry run, apply/readback, idempotent repeat and restoration of the old boolean. Require an explicit unique run-owned scratch root, cleanup limited to that root, and a deliberately failed assertion that exits nonzero. This is write-path evidence, not actual player compatibility.

## Parked Slice 2: targeted metadata cache, not approved by this gate

Use a separate dependent commit/PR for the Compose-only change. Add an explicit META_CACHE_TTL entry targeting preset id `tmdb-addon` at 3600 seconds and wildcard 300 seconds. Before selecting the final map, inspect the current effective environment and nonsecret stored override for this setting. Preserve unrelated keys. If a non-default map is present and cannot be proved preserved, stop this slice and submit the precise delta, not a replacement wildcard.

Verify the actual v2.37.0 parser and normalized preset id in a disposable instance of the exact production image. Use stub metadata upstreams and virtual time where possible. Prove a repeated request within the cache lifetime makes no extra upstream request. Prove that episode metadata refreshes after expiry. Verify selection precedence and show that the catalog TTL and non-target metadata TTL are unchanged. This is a cache-behavior test, not proof of actual new-episode publication timing.

The worker must supply a runnable isolated rehearsal, not just mocks. It must take an explicit unique scratch root and contain only fake credentials. Bind loopback or an isolated Docker network. Use a unique Compose project. Clean only its own resources. Do not run the existing fixed-name smoke stack concurrently. Inspect the harness before executing it. Prove a deliberate assertion failure exits nonzero.

## Review and publication

Use one named acpx Codex worker per issue/worktree. The worker implements, tests and signs one commit. It must not push, open PRs, change production, change policy or delegate recursively. Jarvis verifies, gets Oracle implementation sign-off, then publishes. Use one plan gate and one implementation gate per slice, with at most one focused correction round per gate. A failed gate at that cap is a reported blocker, not permission to bypass review.

Run repository CI locally, including actual Actions through act when the verified Docker runtime supports it. If a CI step cannot run, state the exact limitation in the PR. Run the leak gate before publishing this public repository. Do not commit household names, private hosts, passwords, tokens, snapshots or raw private measurements. Record reviewed plan digest, reviewer model, verdict and bundle digest outside public code where necessary. After landing, compare the exact merged tree to the reviewed tree, verify signature/message/author and issue closure.

The private overlay receives a separate tracked submodule update to the exact reviewed public stack commit. Inspect its current baseline and avoid an unbounded latest-main bump. No DNS, tunnel or Tailscale changes are required.

## Rollout and rollback

Run Slice 1 first from a reviewed staged copy against the existing service. Use its already-installed local settings and credentials. Do not print or copy them into shared evidence. Add an explicit `--root` argument for the existing host stack settings root. Default to the script's repository root. Stage only the exact reviewed CLI and Python helper dependencies in a fresh owner-only task directory. Verify their hashes remotely. Load settings from the existing stack root. Do not copy `.env` or household files. Do not invoke rsync with deletion, change production source files, or call deployment/promotion/restore for this configuration-only operation. No password-manager read is required. Save the exact old boolean and full snapshot. Record the live dry run before applying, freeze its hash outside the applying command, and read back the exact target. Allow the normal application cache refresh interval to observe the change. Verify behavior without restarting services.

Thread the selected settings root through snapshot credential resolution in the reused helpers. They must load the host MDBList key, not the staging directory's settings. Keep existing callers compatible through a default argument, preserve snapshot tokenization and do not mutate process-global credential environment. Support an explicit `--snapshot-dir` using the existing owner-only directory validation.

Slice 2 is not executable under this gate. Before any later cache rollout, supply and rehearse exact recovery for two cases: a failed `deploying` transaction and a completed but rejected transaction. The current staged helper refuses file-only recovery in these states. A prose promise to restore files is insufficient. Preserve the production lock, exact image identity and marker/admission invariants. Do not clear a marker manually, run ordinary deploy, change backup data or improvise a recovery command. Review this concrete recovery artifact and its failure-state evidence before reopening that slice.

For Slice 1, record co-tenant IDs, app image ID, health and restarts. Require them to remain unchanged. Make no environment, Compose, image, database or timer changes. A newer nightly has separate security/resume fixes, but the owner's request to inspect the version and this gate do not authorize its production deployment. Keep candidate qualification separate.

Roll back Slice 1 through the same reviewed boolean CLI with the saved old boolean and a fresh read-only current precondition. Preserve later unrelated changes and verify canonical readback. Full snapshot restore is emergency-only and can overwrite later configuration changes. It needs an explicit state comparison. It is not an automatic rollback path. Retain private snapshots until acceptance. Slice 2 rollback remains unqualified and no cache-TTL deployment is permitted.

## Acceptance and reporting

- Capture paired before/after metadata-only and normal movie/episode detail timings for the same titles, five pairs, recording field selection and cache order. Report medians and maximums, not a cold-cache claim. PASS: normal requests no longer resolve merely because the preference was true, and metadata behavior matches explicit metadata-only requests.
- With two native personas, verify identity, Views, title details, explicit MediaSources/version listing, PlaybackInfo and a small cached stream byte-range read. Include one movie and one regular episode, audio/subtitle metadata and resume read behavior. Do not fabricate watched progress or overwrite live resume points to test writes. PASS: no new failures and usable play URLs. Byte delivery is not actual player decoder acceptance.
- Verify all six personas and the 26 home rows remain available. Compare watch-state counts/identities without changing history. Verify settings, size caps, catalog order, language, credentials and rate limits unchanged except the named setting.
- Repeat Resume/Next Up/Upcoming timings, recording first observed versus repeated calls and upstream errors. Do not promise a speed ratio from cache-warmed sequential tests. The parked TTL slice cannot pass this gate. Its own original review gate additionally requires isolated cache/expiry proof, effective setting readback and its unresolved recovery evidence.
- Verify the public basic API, container health and unchanged co-tenant IDs. Compare source and deployed file hashes.
- Actual Odin/Strand/Nuvio version picker, playback, seek, audio/subtitle controls and visible home rendering are separate device acceptance. State them as pending unless actually exercised. Roll back the relevant boolean promptly if a client reports a regression.
- Report exactly what is live and measured. Keep artwork sizing, missing-logo fallback and any unreproduced 429 limiter listed as not implemented. These need a client trace or a separately reviewed upstream patch, not a misleading claim that all four audit ideas shipped.
