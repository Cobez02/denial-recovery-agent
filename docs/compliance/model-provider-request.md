# Model Provider Configuration and BAA Request

Procedure for engaging Anthropic, PBC (Claude API) as the language model provider under a business
associate agreement, and the configuration confirmed before Plumbline's first request. Plumbline
sends nothing to any model until every item in Part 4 is complete and the Clinic has received written
notice under Exhibit B of its business associate agreement. The provider's signed business associate
agreement governs where it differs from the provider documentation summarized here.

| | |
|---|---|
| Organization | Betz Technology Providers ("BTP") |
| Prepared by | Connor Betz |
| Date | |

## Part 1. Console Enablement

The provider offers two routes: enabling HIPAA readiness in the Claude Console with its standard business
associate agreement, or a negotiated agreement through its sales team.

1. Create a new organization used only for this work. HIPAA readiness applies to the whole organization,
   cannot be disabled once enabled, and the provider recommends separate organizations for HIPAA and
   general use.
2. In the Console, open Data retention (`platform.claude.com/settings/privacy`). An organization
   administrator with the HIPAA management permission sees a "HIPAA compliance" card.
3. Download the Business Associate Agreement and the HIPAA Implementation Guide, have counsel review the
   agreement, and accept it on behalf of BTP as its authorized representative. Enablement is bound to the
   version downloaded; keep a copy of that version.
4. If the card is not available, or the agreement requires changes, send the request in Part 2.

## Part 2. Request to the Provider's Sales Team

Sent through the provider's contact form. The request contains no protected health information.

```
Subject: HIPAA-ready API organization and business associate agreement

Hello,

I own Betz Technology Providers, a ______________ in ______________. We are a business
associate of a chiropractic clinic, providing software that classifies insurance claim
denials and drafts corrected claims and appeals for the clinic's staff to review. The
clinic submits everything itself.

We would like a business associate agreement covering the Claude API and a HIPAA-ready
organization used only for this work. The self-service option in the Console is
______________________ (not available to our organization / available, but we need the
following changes to the agreement: ______________________).

How we would use the API:
- Messages API requests only, from a single workstation in the United States.
- Features: prompt caching, structured outputs (fixed JSON schemas containing no PHI),
  adaptive thinking and the effort setting. No Files API, Batch API, code execution,
  web fetch, MCP connector, computer use or beta features.
- Before each request, our software replaces the patient's name, member identifier,
  account number, address and date of birth, the claim numbers, and the dates recorded
  on the claim, remittance, notes and letters with tokens; masks Social Security
  numbers, e-mail addresses and telephone numbers; and blocks any request in which it
  detects a remaining identifier of those kinds. Other dates, names of other people and
  authorization or payment reference numbers can remain, so we treat every request as
  PHI.
- Model: ______________________. Expected volume: about ______ requests per week,
  about ______ tokens per request.
- Inference and storage must be restricted to the United States, and request data must
  not be accessed from outside the United States.

We would also appreciate written answers to the questions below.

Thank you,

Connor Betz
Owner, Betz Technology Providers
______________________ (e-mail) | ______________________ (telephone)
```

## Part 3. Questions for the Provider

1. Which agreement version applies, and which API features does it cover for the use described above?
2. Under HIPAA readiness, how long are requests and responses retained for our model, and where? (The
   provider documents that certain models require thirty-day retention, and that inputs and outputs may be
   retained for up to two years when its trust-and-safety systems flag a request, or longer where required
   by law.) How is retained data protected and deleted?
3. Does limiting the workspace's allowed inference locations to "us," with "us" as the default, keep all
   inference in the United States, and does a "us" workspace keep stored data in the United States? Is
   request data ever accessed or processed outside the United States, including by subprocessors, by staff
   or during trust-and-safety review of a flagged request?
4. How quickly will the provider notify us of a breach or security incident affecting our data, and
   through which contact?
5. When the agreement ends, what is returned or destroyed, and how is that confirmed?
6. Which subprocessors handle request data, and where are the provider's current security reports
   available?

| Question | Answer | Provided by (name, date) |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |
| 4 | | |
| 5 | | |
| 6 | | |

## Part 4. Configuration Confirmed Before the First Request

| Item | Confirmed on | By |
|---|---|---|
| HIPAA-ready API organization confirmed and business associate agreement active (Messages API requests only; batch processing, the Files API, code execution, web fetch and the MCP connector are outside the agreement and are not used by Plumbline, which also does not use computer use) | | |
| The provider's business associate agreement compared with Sections 2.3, 2.4, 2.11, 2.12 and 5.4 of the Clinic's business associate agreement (breach-notice period, subprocessors and their locations, permitted uses, return or destruction), and the result recorded in Part 3 | | |
| The provider's written answer to Question 3 confirms that request data is not accessed or processed outside the United States, including by subprocessors and staff; otherwise the provider is not engaged unless the Clinic agrees in writing | | |
| A dedicated workspace in that organization, with allowed inference location "us," default inference location "us" and storage location "us," because Plumbline does not set an inference location on its requests and the provider's default is global | | |
| A service account API key created in that workspace (never a key from another workspace or organization, and never a personal key), provided to Plumbline in the CBWORK installation's configuration file or the CBWORK user's environment only; never in code or in the source repository, never logged; no other `ANTHROPIC_API_KEY` defined for the machine, because an environment variable takes precedence over the configuration file | | |
| In a CBWORK command prompt, `set ANTHROPIC` lists no variable other than `ANTHROPIC_API_KEY` (none when the key is in the configuration file); `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS` and `ANTHROPIC_LOG` are set neither for the user nor for the machine, because they would redirect requests, add headers or log request contents | | |
| One request with synthetic data, sent from CBWORK with that key, returns a response whose usage reports inference location "us" | | |
| Model version pinned in `LLM_MODEL` and the prompt regression run against it; automatic model substitution disabled (`LLM_SERVER_FALLBACKS=false`) | | |
| The organization is used for no other purpose; Console access is limited to the Security Official, who signs in through an e-mail or Google account protected by multi-factor authentication (the Console has no sign-in factor of its own); no PHI in the Console or its playground, in workspace names, in user information or in support tickets, which the provider does not cover | | |
| Prompt regression tests run, and the opt-in live test run on synthetic data only; request recording never enabled with real data | | |
| No PHI in output schemas (Plumbline's schema is fixed and contains only category names and field descriptions) | | |
| Decision recorded on model-assisted letter drafting (`MODEL_ENABLED_FOR_DRAFTING`); disabling it at first limits the note text sent, because the model then assists only with classification | | |
| Spending limit set in the Console | | |
| Vendor Register updated with the agreement date, version and the answers in Part 3 | | |
| Written notice given to the Clinic that the model provider is engaged, stating the provider's retention periods, its breach-notification time and the locations from which its personnel and subprocessors may access request data (Business Associate Agreement, Section 2.4 and Exhibit B) | | |
| `LLM_PROVIDER=anthropic` set in the CBWORK configuration, and the first requests reviewed in the audit log (model requests, blocked requests, failed requests) | | |

**Features used by Plumbline and their HIPAA eligibility, as published by the provider:** Messages API
(eligible); prompt caching (eligible); structured outputs (eligible; only the JSON schema is cached, for up
to 24 hours); thinking and adaptive thinking (eligible); effort (eligible). The optional server-side model
substitution relies on a beta interface, and beta features are generally not covered unless listed, so it
remains disabled.

## References

- Anthropic, API and data retention: https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
- Anthropic, Data residency: https://platform.claude.com/docs/en/manage-claude/data-residency
- Anthropic, Authentication: https://platform.claude.com/docs/en/manage-claude/authentication
- Anthropic Privacy Center, Business Associate Agreements (BAA) for Commercial Customers: https://privacy.claude.com/en/articles/8114513-business-associate-agreements-baa-for-commercial-customers
