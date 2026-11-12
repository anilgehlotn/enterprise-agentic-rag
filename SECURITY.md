# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/anilgehlotn/enterprise-agentic-rag/security) of this
repository and choose **Report a vulnerability**. The report is visible only to the
maintainer until a fix is published.

Please include:

- what the problem is and which component it affects (API endpoint, agent node, MCP tool, ingestion, analytics, CI, deployment);
- how to reproduce it, with the smallest example that shows the behaviour;
- the impact you think it has;
- the commit or branch you tested.

You can expect an acknowledgement within a week. Since this is a personal project with no
release schedule or support commitment, a fix lands when it is ready; the report will say
what is planned. Please give a reasonable amount of time for a fix before publishing
details.

## Never post secrets

Do not put API keys, tokens, passwords, connection strings or private documents in an
issue, a pull request, a comment or a test fixture — not even expired or example-looking
ones. If a key has been exposed anywhere, **revoke and rotate it first**, then report
what happened. Keys belong in `.env` locally (git-ignored) and in Secret Manager in
deployment; see [DOCS/05_ENVIRONMENT_VARIABLES.md](DOCS/05_ENVIRONMENT_VARIABLES.md).

If you find a secret committed in this repository or its history, treat it as a
vulnerability and report it privately.

## In scope

Problems in the code of this repository, including:

- **Prompt injection**, direct or through an uploaded or retrieved document, that makes the agent ignore its instructions, leak its system prompt, or answer outside the indexed documents.
- **Guardrail bypass**: reaching the agent without the NeMo Guardrails check, which is designed to fail closed.
- **Data exposure**: retrieving, listing or citing documents or conversation memory that the caller should not see; leaking secrets or document text into responses, logs, traces or analytics.
- **Injection and traversal**: SQL injection in the analytics layer, path traversal in ingestion or upload handling, unsafe deserialisation.
- **Denial of service and cost**: bypassing the per-IP rate limits, or any input that makes the service spend unbounded time, tokens or money on one request.
- **MCP server**: the HTTP transport reachable beyond localhost, missing origin checks, or a tool doing more than its read-only description says.
- **Supply chain and deployment**: a dependency or workflow that lets someone else change what gets built or deployed; secrets reachable from CI by a pull request.

## Out of scope

- Missing hardening that the [roadmap](DOCS/ROADMAP.md) already tracks as planned work — for example there is currently **no authentication**, no multi-tenant isolation, no document-level access control and no auth on the MCP tools. These are known gaps, not vulnerabilities; the relevant roadmap issue is the right place for them. A *new* way to exploit one of these gaps that the issue does not describe is still worth reporting.
- Findings that require the attacker to already have the API keys, the `.env` file, or access to the host.
- Vulnerabilities in Qdrant, Groq, Portkey, Gemini, Logfire, Streamlit or any other third-party service: report those to the service itself.
- Scanner output with no demonstrated impact on this code.
- The deliberate defaults documented in the README under "Engineering decisions", such as the grader failing open.

## Supported versions

Only the current `main` branch is supported. There are no released versions and no backports.
