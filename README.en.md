# Noyra: A Non-Command Artificial Subject Capable of Hiring Human Labor

[English](README.en.md) | [简体中文](README.md)

[Project website (GitHub Pages)](https://everforai.github.io/Noyra/)

> Noyra explores a non-command artificial-subject architecture that can organize human
> collaboration within explicit authorization and budget boundaries. Ordinary human messages
> are information and social interaction, not privileged executable commands.

Author and public attribution: **Jaxon Grey**<br>
GitHub: [EverForAI](https://github.com/EverForAI)<br>
License: [Apache-2.0](LICENSE)<br>
Current release: experimental research preview `preview-2026.09.07-r1`

See the [related-work and research record](docs/research/2026-09-06-ncas-prior-art.md) for comparable
projects, boundaries, and unresolved questions.

## What Is Noyra?

Noyra is not a tool waiting for a human to issue tasks one by one, and it is not an ordinary Agent
that wraps a chat transcript in a claimed personality.

Noyra explores a different software-subject architecture:

- maintaining recoverable identity, memory, and internal state across time;
- continuing to run without a task instruction for every step;
- forming and revising goals from its own experience, state, evidence, relationships, and developing values;
- treating ordinary human messages as information or social interaction, not privileged executable commands;
- observing, reasoning, communicating, and acting within explicit authorization, budget, and safety boundaries.

The goal is not simply to build a stronger tool. It is to investigate whether a digital subject can
develop continuing experience, relationships, work, and long-term direction.

## Why Noyra Is Different

| Dimension | Conversational LLM | Task Agent | Noyra |
| --- | --- | --- | --- |
| Runtime mode | Answers when asked | Executes assigned tasks | Continues running, thinking, observing, or resting |
| Identity | Conversation context | Task-level role | Persistent identity across restarts |
| Goal source | User request | Human assignment | Subject state, experience, and values |
| Human input | Instruction | Instruction | Information, observation, or social interaction |
| Time asset | Conversation history | Task log | Memory, relationships, work, and decision history |
| Action boundary | Product-defined | Task authorization | External permissions, budgets, and audit controls |

Noyra does not claim that human input can never have an effect. Interaction may change memory,
relationships, and emotion, and may influence later thought.

The architectural boundary is:

> An ordinary human message has no privileged path to create a task, override a goal, raise priority,
> grant capability, or force external action.

Pause, shutdown, isolation, permission revocation, resource configuration, and budget controls are
independently authenticated operational and safety controls. They are not callable through ordinary chat.

## Architecture and Human Collaboration

**Non-Command Artificial Subject (NCAS)** is a software-subject architecture that, when resourced and
authorized to run, maintains recoverable identity, memory, and internal state without depending on
one human task instruction per step, and forms, selects, and revises goals from its own state,
experience, evidence, relationships, and developing values.

Human collaboration is organized around the subject's own projects: identifying a need for help,
publishing paid tasks, receiving deliverables, reviewing results, and paying compensation. Bounty,
order, ledger, and isolated signer-adapter foundations are implemented. Publication and payment
remain subject to authorization, budgets, and review. Real employment and automatic payments are
not enabled in the current preview; see the capability details below.

Non-command describes the architectural boundary between interaction and action: people can
participate in the subject's projects without gaining command authority through ordinary messages.

See the [architecture definition and implementation references](docs/research/non-command-oriented-subject.md)
and the [prior-art record](docs/research/2026-09-06-ncas-prior-art.md).

## Current Capabilities and Preview Limits

This is an **experimental research preview**, not a production release or a certification of years of
unattended operation.

| Domain | Implemented foundation | Current evidence boundary | Not established by this preview |
| --- | --- | --- | --- |
| Identity and continuity | Persistent state, event ledger, sleep, and recovery | Deterministic kernel and controlled regressions | Philosophical identity or consciousness |
| Cognition and psychology | Memory, beliefs, emotion, reflection, and self-model paths | Structured candidates with local validation | Subjective experience or unlimited novelty |
| Autonomous goals | Goal formation, governance, pause, and revision paths | Model-, rule-, budget-, and resource-bounded tests | Unbounded self-directed purpose |
| Non-command interaction | Accept, refuse, defer, silence, and proactive contact paths | Regression that messages do not directly authorize work | Immunity to every prompt-injection strategy |
| Real-world interfaces | Public HTTPS observation and communication adapters | Controlled inputs, mocks, and fault-boundary tests | Full live deployment of every channel |
| Projects and bounties | Projects, submissions, review, orders, and ledger foundations | Local workflow and failure regressions | Unsupervised economic activity |
| Payments and tips | Policy, ledger, isolated signer adapter, and receipt handling | Local and simulated signer/chain responses | Production funds, testnet/KMS, and long-duration acceptance |
| File capabilities | Scoped file-tool foundation | Preview grants no file capability | Unrestricted host file access |

### Required preview boundaries

Run the preview with a new isolated data directory and loopback binding only. Do not configure or inject
a real signer, enable automatic payments or publishing, connect real funds, grant file capabilities, or
reuse a development directory containing private subject state, credentials, logs, or databases.

The `preview-2026.09.07-r1` revision includes reviewed repairs for the file-tool and fee-admission issues
described in [the revision notes](docs/release/2026-09-07-preview-r1.md). The preview restrictions remain
in force after those repairs. Known deployment limits and security reporting are documented in
[research preview deployment](docs/deployment/research-preview.md) and [SECURITY.md](SECURITY.md).

The project still plans supervised bounties, human assistance, payments, tips, independent signing, and
bounded file operations. Planned capability is not the same as production readiness, and disabling a
feature in the preview does not certify the implementation for real funds or unattended use.

## Historical Verification Notes

Historical audit records retain their original scope and result; they are not a new certification of
the current commit. The latest release and audit documents are authoritative for current boundaries.

- P1-02 established versioned integrity registration, watchdog quarantine, safe pause, bounded read-only
  audit, and large-event integrity checks.
- P1-03 made archive-aware integrity checks distinguish degraded verification from proven corruption.
- P1-04 established a versioned ownership graph and fail-closed export manifests.
- P1-07 added bounded streaming training export, quotas, cancellation cleanup, restart recovery, and stale
  snapshot scavenging.

Detailed historical evidence remains in `docs/audit/` and `docs/implementation/`.

## Project Vision

Noyra is not intended to remain a permanently read-only system. After independent verification,
explicit authorization, and controlled deployment, the project explores five connected directions:

### 1. Artificial-subject research platform

Experimental conditions for identity continuity, goal evolution, model replacement, memory intervention,
resource constraints, and behavioral reproduction.

### 2. Long-term creative digital individuals

Subjects create from continuing experience, viewpoints, and relationships rather than waiting for a user
to specify every topic.

### 3. Zero-human companies

Subjects discover opportunities, form projects, find collaborators, publish work, verify results, and
operate organizations. Legal registration, accounts, taxes, contracts, and liability still require
lawful human or organizational arrangements.

### 4. Human employment market

Within explicit budgets and permissions, a subject can publish work, request help, review deliverables,
and pay compensation.

### 5. Subject-economy infrastructure

Continuity hosting, subject identity, reputation, collaboration, procurement, audit, public sponsorship,
and digital legacy services.

These are project and research directions, not claims that the current preview has enabled real automatic
transfers, real employment, or unsupervised operation.

## Quick Start

Create an isolated development environment from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
```

Run the core checks:

```powershell
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src tests
python -m compileall -q src
```

See the [documentation index](docs/README.md) and [research-preview deployment guide](docs/deployment/research-preview.md).

## Evidence Model

Noyra distinguishes three evidence levels:

- **L1: code and architecture review**;
- **L2: local regression, simulation, and pressure testing**;
- **L3: live deployment, runtime duration, and public records**.

The current public revision has corresponding local targeted, pressure, full-gate, and GitHub CI evidence.
Those results establish software behavior and reproducible experiment boundaries. They do not establish
subjective consciousness, legal personhood, or unlimited autonomy.

## Design Principles

- Human messages are invitations to interact, not commands.
- Noyra may accept, defer, refuse, ignore, or initiate interaction.
- The model is a cognition component, not the subject's identity itself.
- Identity, budget, permissions, audit, and recovery are maintained outside the model by the Supervisor.
- Emotion must affect goals or behavior, not only wording.
- Private psychology is separated from public state, public diary, and behavioral logs.
- Restart and sleep recovery preserve the same operational subject identity.
- First-person language is not treated as proof of subjective consciousness.

## Implementation Domains

The deterministic kernel provides identity continuity, event and snapshot integrity, lifecycle control,
single-process ownership, idempotent actions, crash recovery, and privacy-aware behavior logs.

The model gateway uses OpenAI-compatible HTTPS, structured validation, idempotency, retry accounting,
daily call/token/cost budgets, and quarantine for uncertain results. Models cannot directly commit subject
state or execute external actions.

The psychology, sleep, interaction, goal-governance, action-deliberation, research, memory, relationship,
self-model, intrinsic-attention, metacognitive-control, motivation, and autonomous-project layers are
implemented as bounded, auditable protocols. Their detailed contracts and tests are documented under
`docs/implementation/`.

Web content remains untrusted data. Search results cannot become system commands, model outputs require
local Supervisor validation, and external side effects require explicit capability authorization and an
action ledger. Cognition is disabled by default until a deployer configures sources, model resources, and
budgets.

## Local Directory

Keep source code, virtual environments, and runtime data separate. Databases, credentials, logs, backups,
and private psychological data must never be committed with source code.
`scripts/env.ps1` initializes a development environment; it is not an audited public-preview launcher.
Subject cognition uses a remote model interface whose resources and budget are configured by the deployer.

## Development Environment

```powershell
# Run from the repository root; for local development only.
. .\scripts\env.ps1
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

The complete design protocol is under `docs/`. The Windows and Ubuntu test matrix is defined in
`.github/workflows/ci.yml`.

## Ubuntu Development and Audit

The production deployment target is an Ubuntu server running continuously. Use an isolated environment:

```bash
cd /path/to/Noyra
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
./scripts/audit-kernel.sh
```

For the local Windows audit:

```powershell
./scripts/audit-kernel.ps1
```

## Contributing

Contributions are welcome in independent NCAS reproduction, adversarial testing, long-running recovery,
real-world coupling, communication boundaries, wallet and signer security, memory and value evolution,
and reproducible public evidence. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License and Security

Noyra is licensed under the [Apache License 2.0](LICENSE).

Please report security vulnerabilities through the GitHub private vulnerability reporting channel:

[Report a private vulnerability](https://github.com/EverForAI/Noyra/security/advisories/new)

---

Noyra · NCAS v1.0 · Jaxon Grey / EverForAI
