# A.U.R.A. CAD Design

## Architecture

```text
typed / voice request
        |
actions/cad_design.py -> aura_cad/integration/hooks.py
        |                     |
        |                     +-- quiz-style, one-question slot filling
        |                     +-- floating choice card with spoken answers
        |                     +-- existing confirmation and undo brokers
        v
CadService -> deterministic engine template OR existing Gemini/model router
        |
        +-- AST allowlist -> isolated Python worker -> build123d STL
        +-- trimesh checks -> STL / OBJ / GLB -> versioned user-data project
        +-- existing floating browser -> bundled local WebGL viewer
```

Clarifying questions and confirmations are deterministic; they make no model
call. Common engine builds use a local parametric template for Inline, Flat,
and V-type layouts, avoiding planner, code-generation, and repair requests.
Other object types use planning, script generation, and bounded repair through
the existing `cad_design` deep task tier. Engine template failures are reported
directly instead of triggering an LLM repair. The viewer has no CDN, telemetry,
or remote request.
Each clarification is shown in a floating card and spoken by AURA. The user can
click an option or answer with its ordinal (for example, "the second option");
typed replies are accepted through the same clarification flow.

Complex CAD requests use the deep task route. It checks the API key's model list
and only attempts `gemini-3.8-flash`, `gemini-3.6-flash`, and the free-tier
`gemini-3.5-flash-lite` fallback when those exact model IDs are listed as
supporting content generation. If discovery fails, the route fails closed to
local fallback/retry; it does not hand deep requests to the older legacy Gemini
ladder.

Google's [current pricing page](https://aistudio.google.com/docs/pricing) lists
Gemini 3.8 Flash, 3.6 Flash, and 3.5 Flash-Lite as free of charge on its Standard
Free Tier. This is not a promise that those prices or limits will remain
unchanged. A.U.R.A. cannot inspect whether the API key's Google project has
billing enabled. To avoid charges, use a project that remains on the Free Tier
with no billing account attached; no client-side router can guarantee that a
provider will keep a service free forever.

## Local part cache

The optional part cache reuses successful parametric part functions from earlier
builds. It matches normalized part signatures first, then uses a conservative
pure-Python feature-token similarity score (default threshold `0.88`). A match
is adapted only when the requested changes fit the cached typed parameter
schema; structural changes fall back to normal generation. The cache is CPU-only
and works offline. No prompt, project, or cache data is sent to a cache service.

Every cached function is integrity-checked when loaded, revalidated by the CAD
AST validator on each reuse, and run through the same sandbox, mesh checks, and
exports as generated code. Only functions used in a fully successful build can
be admitted. Files are stored under `%LOCALAPPDATA%\AURA\cad\part_cache\` on
Windows (or `~/.aura/cad/part_cache/` when `LOCALAPPDATA` is unavailable);
corrupt entries are moved into its `quarantine` folder. The settings overlay
shows entry count and size, has an on/off switch, and offers **Clear part cache**
behind the existing confirmation menu. Clearing it does not affect project
history or undo.

To add a backend or prompt compatibility rule, update
`PART_CACHE_VERSION_COMPATIBILITY` in `aura_cad/config.py`. Entries require an
exact version match unless the table explicitly marks the stored version as
compatible with the current one. Cache limits and the conservative match
threshold are configured there as well. Fuzzy matches can be wrong, so keep the
threshold conservative; a rejected or unsafe entry falls back to generation.

## Dependencies and runtime support

Install the opt-in dependencies with `python -m pip install -r requirements-cad.txt`.
The verified package metadata and Windows wheel set at implementation time cover
Python 3.10, 3.11, 3.12, 3.13, and 3.14: build123d 0.12 accepts Python 3.10–3.14,
cadquery-ocp-novtk 7.9.3.1 and fast-simplification 0.2 provide cp310–cp314
win_amd64 wheels, and trimesh is pure Python. A.U.R.A. does not install these
packages automatically. If a required backend package is missing, CAD requests
offer installation through the existing confirmation gate; declining leaves the
rest of A.U.R.A. unchanged. Oversized meshes are simplified locally before the
triangle limit is applied.

## Add an object template

Add a tuple of `Slot` definitions to `aura_cad/spec/templates.py`. Keep questions
short and include only fields supported by the code generator. Add parsers for
any new numeric or categorical slot to `aura_cad/spec/clarifier.py`, then cover
the new answer variants with tests.

## Add a viewer command

Add the command to `aura_cad/viewer/bridge.py` and implement it in the local
`aura_cad/viewer/assets/viewer.js`. The bridge accepts a fixed command allowlist;
do not add remote scripts, URLs, or generic JavaScript execution.

## Safety and troubleshooting

Generated code is parsed against a narrow AST/import/API allowlist before it is
run in an isolated child process. The child has a hard deadline, capped output,
and a Windows Job Object process-memory limit where the host permits assignment;
other platforms use `resource.setrlimit` when available. This is defense in
depth, not a perfect sandbox. Never add a setting that bypasses validation.

If CAD is unavailable, install `requirements-cad.txt` after approving the
confirmation. If the viewer says WebGL is unavailable, update the embedded
browser runtime or enable its software/hardware WebGL implementation. Build
failures are shown briefly in the HUD; the `aura.cad` logger records job-scoped
diagnostic details.

## Known limits

Code-CAD works best for mechanical and geometric parts, not organic shapes. An
"engine" is a convincing simplified model, not manufacturing-grade engineering.
Generated code executes locally, so the validator and sandbox are mandatory.
While a model is being built, A.U.R.A. displays an estimated stage-based
progress card with a cancel control. The estimate advances as planning, geometry,
validation, export, and saving complete; it is not a measurement of CAD solver
work within a stage. The card periodically updates while a long stage is running.
Clarification polls also use a floating card; AURA speaks the question and its
options, and answers can be selected by click or spoken ordinal.
The lightweight built-in WebGL viewer loads STL; STL, OBJ, and GLB are all saved
with each successful project version. Exploded view separates triangle facets
for visual inspection, not assembly analysis.
