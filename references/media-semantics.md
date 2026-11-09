# Frozen media semantics

These rules interpret a Task's explicit semantics. They do not grant permissions,
change the user's goal, or weaken any frozen acceptance criterion.

## Required processing methods

A user-required provider, model, tool or processing method constrains the
derivation of the delivered artifact, not merely which tools were attempted.
A failed or rejected provider call does not establish provenance for a later
local replacement made from the original source. Better appearance or stronger
pixel preservation does not waive the named method without an explicit applied
user change. Use current delivery_provenance and input bindings; follow the
bound upstream producers for local cleanup. A succeeded registered local
operation's operation_execution_contracts (implementation, lifecycle and
model_profile) and accepted outputs establish its local processing method.
Local model operations do not create external provider receipts: an empty
receipt list is expected, not evidence that the local model was skipped.
For external operations use matching provider receipts. A declaration or a
failed/pending operation alone does not prove execution. Do not infer the
final method from unrelated global run history.

Review and diagnostic feedback identifies defects; it is not authority to
change a user-required processing method. A registered implementation chosen
by the Planner is not automatically a user-required method. Within existing
permissions, an equivalent local implementation may satisfy the same output
and style requirements without asking for new authorization. A requested
visual style alone does not require retaining the first renderer selected.
Step reviewers may not see every Run requirement, so describe the property
needing correction and make implementation alternatives conditional on the
Planner preserving explicit user method constraints. Obtain a method change
only when a user-required method would change, or additional permission is
actually needed. Merely borrowing a candidate's color statistics is not
equivalent to compositing that candidate's edited pixels when the latter is
what the user requested.

When a required-method candidate needs local cleanup, bind that verified
candidate or its mask as an input to the authorized repair. Preserve its
derivation instead of generating a new substitute from original pixels alone.
If the required method cannot meet the goal or is unavailable, explain the
specific limitation and request the user's method decision before changing it.

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

For background-only editing that must preserve an existing foreground's size,
position and shape, provide an explicit background selection to the editor so
the host can preserve pixels outside it. Text-only preservation requests to a
whole-image generator do not provide that spatial guarantee.

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

Make qualitative camera motion concrete in the generation prompt. A slight
push-in can use a small total scale change (for example 5–10% when no amount is
specified), while keeping required reference features in frame. Explicit user
amounts take precedence; smooth motion alone does not establish a small amplitude.
An example generation target is not an additional user acceptance threshold.
When the user specifies only qualitative motion, preserve that qualitative goal
and required framing in acceptance; do not turn an illustrative percentage into
a mandatory numeric bound. Explicit user bounds and already frozen acceptance
remain binding.

Use timestamped observations covering onset, middle and ending. A single still
frame cannot establish motion, continuity, or a gradual transition. Distinguish
observed contradictions from insufficient evidence; report the latter explicitly.
Inspect enough temporal evidence within the frozen inspection budget before
finalizing; if that cannot be done, do not fabricate a positive conclusion.

## Evidence and repair

Keep process requirements, interface capabilities and artifact contents distinct.
An approved-plan receipt proves that a plan was shown before execution; it does
not require exporting a plan document unless the user asked for one. A PNG can
be loaded as a raster image layer and manipulated by the image workbench; that
does not make its text or adjustment parameters editable. Statements that the
interface supports manual selection, layer positioning or importing results are
not requests to embed those interface controls into each output. Do not claim
that a UI action was performed merely because the capability exists.

Editable behavior requires an implementation that consumes the delivered
parameters, not just parameter names or values saved beside a rendered image.
For parametric adjustments, deliver a supported native document with its source
assets, or a runnable renderer plus parameter file and necessary sources. Verify
in a clean directory that the default settings reproduce the preview, disabling
all adjustments restores the base, and changing or disabling each requested
adjustment changes the output. A self-check claiming these properties without a
usable reader/renderer is insufficient evidence.

OpenRaster baseline image layers expose position, opacity, visibility and blend
modes; they do not by themselves implement contrast/saturation/temperature
parameters. Custom XML attributes or an `adjustments.json` member require an
actual compatible reader. Approximate raster blend proxies are not evidence that
the saved adjustment parameters can be edited and re-rendered. Inspect the
supplied implementation or report the missing functional evidence. See the
[OpenRaster layer-stack specification](https://www.openraster.org/baseline/layer-stack-spec.html).

Deterministic measurements establish geometry, stream presence, full-stream silence,
loudness, text sequence and mask changes. A bounded audio or video sample does
not establish an unobserved interval. Tone estimates are for stable narrow-band
signals, not general music or speech identity.

Equal gain values do not establish unified loudness: leaving every clip at
`volume=1` preserves its original level. A request to normalize/unify loudness
requires an actual normalization process or measured evidence that the source
already satisfies the requested target. Read the executed clip/master filters
and output-bound report; program loudness normalization does not require every
short window or every spoken word to have identical RMS. Keep per-clip edge
fades separate from whole-output fades and lossy-encoder peak changes.

An unreadable thumbnail is insufficient evidence, not proof that small text or
an annotation is absent. Use a bounded `media.evidence` crop when necessary.
Transcript coverage gaps are not measured acoustic silence: words and detected
audio events may cover an interval continuously. Distinguish zero gaps from a
missing transcript or an omitted computation.

For a trimmed audio candidate, `media.evidence` audio_compare can use offset for
the candidate and reference_offset for the corresponding original-source time.
Its stable-tone measurements can support a narrow-band frequency comparison;
null tone values do not establish speech pitch preservation or source identity.

Automatic repairs keep the original goal, authorization and acceptance. Explicit
user feedback can revise the allowed affected closure, but still cannot grant new
assets/capabilities or bypass technical and Agent checks. ManagerResult states the
outcome explicitly; a new node only replaces an old failed requirement when the
Proposal records a legal `replacements` relation. Renaming tasks or increasing a
graph version does not create new evidence or authorize another content review.
