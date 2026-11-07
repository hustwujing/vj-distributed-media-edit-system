# Frozen media semantics

These rules interpret a Task's explicit semantics. They do not grant permissions,
change the user's goal, or weaken any frozen acceptance criterion.

## Subject scope

`subject_cutout` includes the subject's body, clothing and worn accessories by
default; held and carried objects are excluded. `foreground_cutout` includes held
and carried objects. Explicit include/exclude relations override those defaults.
Do not infer that an object belongs to the requested subject merely because its
pixels touch the subject. If the requested relation is ambiguous and affects the
result, ask for clarification through REQUEST_USER_INPUT.

Inspect the source and the resulting alpha/mask. Alpha-channel presence alone
does not prove transparent background, and a transparent border does not prove
that the correct subject was selected. Compare semantic extent independently of
image geometry and alpha topology.

## Generated subject consistency

`strict_reference` requires preservation of the reference identity and explicit
appearance constraints. `character_consistent` (the default) preserves recognizable
identity and distinguishing features while allowing pose, lighting, perspective,
and normal temporal changes. `creative` permits intentional variation within the
user's description. Do not silently upgrade character consistency to pixel identity
or downgrade an explicit strict requirement because generation is difficult.

Use `media.evidence` with the original reference image when helpful. Its luma/edge changes and reference comparisons are measured structure/appearance facts, not an identity score. Use actual reference and output evidence. If the evidence cannot establish
identity, report inconclusive; do not claim an objective identity score.

## Local mask repair

A: small edge defects -> local edge repair. B: small interior holes -> local hole
repair. Both require an explicit maximum changed-pixel fraction relative to the reference subject area (not the full canvas) and declared
reference/output mask roles. C: major missing/extra regions -> resegment with an
authorized capability. D: unclear semantic extent -> clarify scope before execution.
Do not repair a semantic selection error by repeatedly eroding or dilating a mask.

The program verifies declared local-change limits. The Reviewer still checks
whether the repaired mask preserves the intended subject and relations; topology
metrics alone cannot establish semantic correctness.

## Temporal actions

Express requested timing in `semantics.temporal`. Defaults for an immediate action
are onset by the earlier of 1 second or 10% of duration. A continuous action uses
at least 85% coverage and no interruption longer than 0.5 seconds. A gradual
transition occupies at least 50% of duration without an abrupt jump unless the
user explicitly permits one. Preserve explicit user thresholds over defaults.

Carry the frozen onset and continuity requirements into the video-generation
prompt itself; acceptance fields alone do not instruct the generator. If the user
requires a particular starting pose in the reference image, preserve that image
requirement and describe the transition to the action within the onset deadline
in the video prompt.

Use timestamped observations covering onset, middle and ending. A single still
frame cannot establish motion, continuity, or a gradual transition. Distinguish
observed contradictions from insufficient evidence; report the latter explicitly.
Inspect enough temporal evidence within the frozen inspection budget before
finalizing; if that cannot be done, do not fabricate a positive conclusion.

## Evidence and repair

Deterministic measurements establish geometry, stream presence, full-stream silence,
loudness, text sequence and mask changes. A bounded audio or video sample does
not establish an unobserved interval. Tone estimates are for stable narrow-band
signals, not general music or speech identity.

Automatic repairs keep the original goal, authorization and acceptance. Explicit
user feedback can revise the allowed affected closure, but still cannot grant new
assets/capabilities or bypass technical and Agent checks. ManagerResult states the
outcome explicitly; a new node only replaces an old failed requirement when the
Proposal records a legal `replacements` relation. Renaming tasks or increasing a
graph version does not create new evidence or authorize another content review.
