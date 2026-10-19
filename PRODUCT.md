# FilmDust Product

<!-- impeccable:product-schema 1 -->

## Platform

web

Existing delivery is a local web editor embedded in a macOS AppKit/WKWebView shell, plus a browser entry point. The immediate product target is the owner's own Mac workflow. This platform label describes the existing interface implementation; it does not imply a hosted website or a new mobile app.

## Users

A Mac user processing personal film-scan photographs, including both moderate-resolution previews and full-resolution originals. Performance on smaller scans cannot stand in for larger originals. Public distribution, scan-shop workflows, and batch processing are outside the current iteration.

## Product Purpose

Delegate dust removal to the machine: import a photograph, automatically identify and remove dust and fine strands, quickly view a useful result, and save a copy. Repeated parameter setup and extensive manual correction defeat the primary purpose.

## Positioning

A focused local film-scan cleanup tool. Product claims must be supported by actual image results and native-App behavior. Candidate counts and repair counts are not recognition accuracy.

## Operating Context

- Main input: scan JPEGs, including full-resolution originals around 60 megapixels.
- Workflow: configure once, process photographs one at a time, inherit preferences when moving to another photograph.
- User-reported pain: slow identification, many missed dust spots/fine strands, repeated settings after switching images, and three strength tiers with little practical value.
- User accepts higher false-positive risk in exchange for more complete automatic cleanup. This explicitly supersedes treating the current conservative default as the desired product policy.
- Expected review burden: a quick look at the comparison and occasional restoration of an individual mistaken repair; no required item-by-item approval.
- Acceptance target: at most 10 seconds from confirming an import to viewing the complete usable cleanup comparison on the owner's representative JPEG input and Mac. This is a target, not a verified current capability or a guarantee for every supported size.

## Capabilities and Constraints

Current implementation supports single-photo automatic processing, comparison, manual add/restore/erase/clone corrections, undo, and copy export. Existing code offers JPEG/PNG/single-page integer TIFF input under file/pixel limits, and TIFF/PNG/JPEG output with format-specific restrictions. The five reviewed import/export and failure-recovery issues have regression coverage in version 1.1.0. Browser failure-injection checks and mounted-App verification passed on the owner's Mac.

Continue the existing local-processing and original-preservation model. Preserve existing supported-format behavior where correct; unsupported input must fail explicitly rather than silently lose information. No new cloud service, model download, batch queue, or public-release commitment has been agreed.

The existing interface and private local-development assets are evidence, not permission to publish personal photographs. No new aesthetic direction has been requested.

## Brand Commitments

Existing name: FilmDust. Existing UI language: Chinese. Existing interface: photo-focused, blue/neutral controls with light/dark appearance. Preserve this identity during the current refinement unless the owner requests a change.

## Evidence on Hand

- README, Python processing/server source, web UI, and native Mac launcher in this repository.
- Regression tests cover input precision, TIFF structure, transparency, failure recovery, stale exports, settings, explicit local repair, cropped previews and exact undo.
- The bundled example is synthetic. Private development photographs, screenshots and machine-specific diagnostic records remain local; private fixture tests are opt-in through environment variables.
- Version 1.1.1 caches the displayed repair result, applies explicit manual operations locally, transfers cropped preview updates and restores cached pixels on undo. Manual confirmation takes priority over automatic keep-original rules.
- Native integration verification exercises the bundled engine, interface, close guard, settings bridge and actual TIFF export. A bounded wait prevents suspended animation frames from leaving controls locked.
- Full-resolution originals can still exceed the 10-second whole-image target. Local-repair timing must be reported separately from complete import timing. Functional checks and changed-pixel counts do not establish identification accuracy.

## Product Principles

1. Optimize for an actually cleaner photograph and less user work; do not substitute detection counts or more controls for a useful result.
2. Prefer more complete automatic dust/strand removal with practical per-region recovery, rather than requiring the user to repair a long list of retained candidates.
3. Never silently discard source precision, corrupt export structure, or let a successful save differ from the displayed result revision.
4. Carry preferences between photographs; keep photo-specific selections, samples, and history isolated.
5. Measure the entire native user journey and judge quality with fixed image comparisons, not algorithm-only timing or synthetic examples alone.

## Implementation Agreement

The owner approved implementation on 2026-10-19 with Impeccable: replace the three main-screen tiers with one automatic-cleanup workflow, persist preferences across images and App restarts, and protect unsaved results on switching and closing. Exact detection changes and performance claims require implementation evidence; candidate counts are not accuracy percentages.
