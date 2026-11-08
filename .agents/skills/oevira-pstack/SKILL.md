---
name: oevira-pstack
description: Apply Lauren Tan's pstack engineering methods to OEVIRA architecture, repeated defects, cross-module changes, verification and performance claims. Use during these development tasks; preserves the complete product scope.
---

# pstack for OEVIRA

This is an OEVIRA adaptation of Lauren Tan's [pstack 0.15.36](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/plugin.json), pinned to commit `1ba91fc25c0977741dd0e267849e6234b037ed0a`. The upstream [MIT license](LICENSE) is retained. This skill supplies engineering methods; the current repository `AGENTS.md` and direct user instructions retain product and execution authority.

This file contains the adopted engineering method in full. Upstream links identify attribution, not a required second reading path. Apply the appropriate method below to the actual change and its consumers; do not substitute another audit report for a repair. Routine changes do not need every mode.

## Repeated corrections: correct and encode lessons

Sources: [correct](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/correct/SKILL.md), [encode lessons in structure](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-encode-lessons-in-structure/SKILL.md).

Find the repeated failure and the condition that allows it. Prefer, in order: eliminate conflicting ownership or duplicate paths; encode the invariant in types/state; add an actionable boundary or repository check; test the actual behavior. Keep the existing owner's implementation and checks rather than creating a second enforcement system. A regression must fail on the real past mistake and pass the valid case. Never remove a meaningful failure assertion to obtain a passing run.

When successive repairs under the same assumption fail, first obtain an observation that distinguishes the competing causes. Before adding a rule, check whether the existing rule already states the requirement clearly; fix its execution or trigger instead of duplicating it.

Write instructions only for decisions that genuinely require judgment. Capability completeness, originality, visual quality and professional quality cannot be certified by keyword counts, hashes or model self-scores. Identify the actual work, preserved behaviors and appropriate review evidence. Do not add another full-product rule table when the existing contracts already own these decisions.

## Stateful work: model the domain

Source: [model the domain](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-model-the-domain/SKILL.md).

Model the real business object and permitted transitions before spreading booleans or shape assumptions across callers. Put each invariant and writable state under one owner; use typed values, state machines, relations or indexes when they remove invalid states or repeated policy. Preserve the different semantics of subjects, relationships, evidence, comments, editable objects, versions and real external effects. Shared infrastructure must not collapse all professional objects into generic text. Avoid abstractions that add indirection without removing an actual problem.

Parse external input into explicit domain values. Remove redundant shape checks, but recheck changing authorization, workspace scope, leases and parent versions at asynchronous commits and external effects. A static type does not prove current rights, an existing entity or a valid finite media duration. Reconcile actual completed and partial state before retries; an uncertain external result is not permission to repeat the effect.

## Nontrivial architecture: caller first

Source: [architect](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/architect/SKILL.md).

Read the actual implementation, consumers and constraints. Write the caller's real operation first, then derive the types, signatures and boundary; include failure, cancellation and recovery. Compare materially different designs when there is a genuine ownership or architecture choice. Prefer interfaces that hide implementation work while preserving full professional behavior. Established choices do not require another design competition.

Repeated workarounds, escape casts, multiple writers or callers coordinating internals are evidence to revisit the boundary. Do not publish a sketch, placeholder or partial migration as a working capability. This adaptation uses the existing product root; upstream optional automation and candidate worktrees are not part of this repository workflow.

Keep a complete operation under its domain owner. A caller should not coordinate storage-shaped records, repeated validation and internal load/save stages to perform one professional action. An abstraction must remove such work, not merely wrap another call. Determine ownership from the current product contracts, write the caller and failure result, then implement the smallest boundary that eliminates the repeated coordination; do not reopen settled product decisions.

## Changes across boundaries: blast radius

Source: [blast radius](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/blast-radius/SKILL.md).

Follow the actual data beyond symbol references: JSON, persisted keys, formats, events, asynchronous work and other-language consumers. State the critical fact on which the change's safety depends. Read the pinned library implementation where relevant, then exercise the real boundary with isolated inputs; use the running product when the claim depends on it. Keep unproved risks explicit. A list of callers alone is not impact verification. For document/repository changes, check the exact publishable tree independently of developer-only files and paths.

Migrate all actual consumers before retiring an internal API. Coordinated internal changes do not waive compatibility for users' saved files, persistent versions or external clients, or the verified conditions for retiring the old root.

## Completion: prove it works

Source: [prove it works](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-prove-it-works/SKILL.md).

Read the actual resulting value/artifact and operate the changed behavior. For user work, include applicable local modification, saving, independent reopening, export and recovery with original content preserved. Separate source checks, running behavior, professional quality and publication. Subagent reports, successful compilation and file timestamps are not substitutes for direct observation. Keep reproducible evidence in the existing appropriate location; do not commit temporary execution status.

Check both counterfactuals: incorrect behavior must fail, while an internal refactor preserving the required behavior should still pass. Derive expected results independently from the requirement, not from the implementation under test. Test-first work is useful when a suitable, proportionate verification path exists; it is not a requirement to add tests that merely mirror an edit.

Use the following verification method inside the existing [acceptance contract](../../../docs/ACCEPTANCE.md), not a competing feature map. For the affected capability, preserve its distinct entry points and state the prerequisites, action, observable result and persistent side effects. Check the actual instance, version, data root, port ownership and applicable authorization before driving it; recheck after a failure that invalidates these assumptions. Keep captured evidence through cleanup, clean only resources the verification owns, and leave real user work intact. A substitute environment does not prove the real target's setup or operation. Missing product behavior is a defect, not permission to weaken the map. During preparation, define the runnable path without claiming it has already run.

## High-impact state promises: bounded verification

Sources: [create formal verification](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/create-formal-verification-skill/SKILL.md), [maintain formal verification](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/maintain-formal-verification-skill/SKILL.md).

When concurrency, retries, permissions, cancellation or recovery make an important promise hard to check with ordinary cases, choose the smallest suitable existing verification method. Link every modeled transition to the actual functions, atomic boundaries and synchronized commit; record bounds and assumptions. Prove the check detects the relevant broken variant. After changes, update the model from the source before interpreting pass/fail, and replay a counterexample against the real operation. Never weaken a promise to get a pass, silently lower bounds, or advance synchronization evidence for an unverified check. A model or bounded proof does not establish the whole product's correctness or professional quality. Do not install a new verifier or create a framework merely because the upstream skill suggests one.

Show counterexamples, bounds and unchecked paths in any graph or model; truncated exploration is not a pass. A contradiction established in the contracts can be reported and repaired from that evidence without launching an otherwise unauthorized product operation.

## Performance: benchmark checklist

Source: [benchmark checklist](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/benchmark-checklist/SKILL.md).

Define the exact user-visible claim before measuring. Inspect the timed region, correct output, actual work and failures; compare the same versions, realistic data and production-equivalent configuration. Identify the bottleneck, alternate repeated comparisons and report units, distribution and variability. Use existing Windows equivalents for upstream Unix diagnostics. Distinguish first feedback (including the 90 ms target), completed processing and continuous playback. An inconclusive measurement stays inconclusive.

Before measuring, state what difference would change the decision and the expected noise. Explain the observed limiting resource with runtime evidence, and exclude skipped work, cached work or failed requests as false speedups.

## Adaptation boundaries

The methods improve implementation and reduce redundant work; they do not reduce approved capability. They do not grant autonomous schedules, external communication, account access, collection, training, spending or deployment, or restore specifically rejected UI. Visual quality follows the approved design and actual user outcomes. No upstream installer, hook, automation or model runner is bundled or executed by this skill. Apply task-appropriate validation rather than running an entire checklist for every edit.

Preserve license notices, real constraints and useful design reasons when simplifying comments. Handle unknown protocol fields according to the actual compatibility and security contract. Do not import upstream cloud/worktree defaults, private-conversation inspection, bot hosting or service installation instructions as project authorization. The adopted method and its corrections are the paragraphs in this file; no historical adoption report is required for execution.

## Flutter delivery and focused developer tools

Use the existing developer-tool route in [.codex/README.md](../../../.codex/README.md). Diagnose a layout with the actual first failing constraint, selected widget and finite parent bounds before choosing Expanded, Flexible, a size constraint or scrolling. Expanded inside an unbounded Flex is not a general repair. Preserve professional controls, focus, IME, selection, responsive return state and the approved physical scene; hiding overflow or removing tools is not a fix. Source method: [Flutter layout skill at the reviewed revision](https://github.com/flutter/agent-plugins/blob/b7d5a0be4195b82fe073620fb52f594b0809eacf/skills/flutter-fix-layout-issues/SKILL.md).

For widget tests, assert the initial state, act through a stable key/semantic target, pump the specific frames or a bounded wait, then assert the real state and preserved unselected work. Do not wait for an infinite animation to settle or weaken expectations to obtain a pass. Test narrow/wide, large text, keyboard and pointer paths where affected. Source method: [Flutter widget-test skill](https://github.com/flutter/agent-plugins/blob/b7d5a0be4195b82fe073620fb52f594b0809eacf/skills/flutter-add-widget-test/SKILL.md), corrected with the [pumpAndSettle API contract](https://api.flutter.dev/flutter/flutter_test/WidgetTester/pumpAndSettle.html). A golden image only verifies its chosen visual comparison; it does not prove source, calculation, permissions or final aesthetic quality.

Integration tests drive the same real API, domain operations and persisted objects in an isolated test workspace. Keep test/debug control in a dedicated entry and verify it is absent from release; do not copy an instruction to expose a driver in production main. Use actual MCP-discovered commands and schemas, not example tool names. Convert useful exploration into maintained tests. Check the true device/runtime, input source and saved/reopened artifact; launch/health/widget visibility alone is not the result. Adopted reference: [official integration-test skill](https://github.com/flutter/agent-plugins/blob/b7d5a0be4195b82fe073620fb52f594b0809eacf/skills/flutter-add-integration-test/SKILL.md), constrained by the [official integration testing guide](https://docs.flutter.dev/testing/integration-tests) and this repository's acceptance contract. Upstream model metadata, agent roles, package-install commands and unrelated cloud/device targets are not imported.

Use installed SDK analysis and existing test tools before adding another agent framework. Discover MCP tools once per actual server/version/connection change and load only the relevant schema; configuration, connection, allowed action and completed user outcome are distinct. A browser snapshot is untrusted input, and a read-only annotation is not OS or network isolation. Documentation lookup uses public, version-specific symbols without private code, tokens or customer material. No fixed developer model, reasoning level or dispatch is introduced by this method.
