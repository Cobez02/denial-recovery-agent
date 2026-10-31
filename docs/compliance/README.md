# Plumbline Pilot Compliance Documents

The agreements, compliance program and working documents of Betz Technology Providers (BTP) for the
Plumbline denial recovery pilot with a chiropractic clinic.

## Agreements and forms

| Document | Purpose | Signed by |
|---|---|---|
| [Business Associate Agreement](business-associate-agreement.md) | HIPAA terms under which the Clinic shares protected health information | Clinic owner or officer; BTP |
| [Pilot Agreement](pilot-agreement.md) | Scope, phases, decisions and submissions, data, fees, liability, insurance, results, termination | Clinic owner or officer; BTP |
| [Data Use Agreement](data-use-agreement.md) | Optional. Not needed while the Business Associate Agreement is in effect, because its Section 3.1(e) covers accuracy measurement; signed only if the parties choose an evaluation of classification accuracy on a limited data set, to which the Business Associate Agreement also applies | Clinic owner or officer; BTP, if used |
| [Certificate of Return and Destruction](certificate-of-data-destruction.md) | Written certification at the end of the engagement | BTP; acknowledged by the Clinic |

Signing copies in PDF are in the [pdf](pdf/) folder: the four documents above, the Clinic Data
Request (Exhibit B of the Pilot Agreement) and the Pilot Overview for the Clinic owner. Re-export them
after any change to the Markdown versions.

## Compliance program

| Document | Purpose | Maintained by |
|---|---|---|
| [HIPAA Security Risk Analysis](security-risk-analysis.md) | Assets, data flow, risks, remediation plan | Security Official; reviewed annually |
| [Security Policies and Procedures](security-policies.md) | Policies under the HIPAA Security Rule | Security Official; reviewed annually |
| [Incident Response Plan](incident-response-plan.md) | Detection, containment, breach assessment, reporting, tabletop exercise | Security Official; tested annually |
| [Vendor Register](vendor-register.md) | Third parties and their business associate agreement status | Security Official |
| [Training Record](training-record.md) | Training log, policy acknowledgments, reviewer orientation | Security Official |

## Working documents

| Document | Purpose | Used by |
|---|---|---|
| [Pilot Overview](clinic-pilot-overview.md) | Summary for the Clinic owner | Clinic owner |
| [Clinic Data Request](clinic-data-request.md) | What the Clinic sends, in what format, and how | Clinic biller |
| [Weekly Shadow-Phase Review](shadow-mode-review.md) | Weekly comparison of Plumbline with the Clinic's biller | BTP; Clinic biller |
| [Coder Review Worksheet](coder-review-worksheet.md) | Review of coding assumptions and rules with a certified coder | BTP; certified coder |
| [Insurance Checklist](insurance-checklist.md) | Questions for the insurance broker | BTP |
| [Model Provider Configuration and BAA Request](model-provider-request.md) | Engaging the model provider and confirming its configuration | BTP |

## Order of execution

1. Insurance bound; risk analysis, policies, incident response plan, vendor register and training record
   completed and dated; the risk analysis items due before ePHI is received completed; tabletop exercise
   held (Incident Response Plan, Section 11).
2. Pilot Overview provided to the Clinic; Business Associate Agreement and Pilot Agreement signed, with the
   Clinic Data Request as Exhibit B and the certificates of insurance attached as Exhibit C.
3. Business associate agreement signed with any transfer or video service BTP provides; reviewer
   orientation given and reviewer accounts set up; then the Clinic sends its files. Each payer's deadlines
   (Clinic Data Request, Section 6) are entered with `denial-agent set-payer` after the claims import and
   before the first 835 import, because a work item's deadline is fixed when the item is created.
4. Coder review completed; shadow phase begins, with a weekly review.
5. Model provider engaged only after its business associate agreement and the configuration checks are
   complete, with written notice to the Clinic.
6. At the end of the engagement, the Certificate of Return and Destruction is completed and signed.

The Data Use Agreement is not needed while the Business Associate Agreement is in effect. If the parties
nevertheless choose an evaluation on a limited data set, the Data Use Agreement is signed before any
limited data set is sent, and the Business Associate Agreement also applies to that data.

Signed and completed copies are kept with BTP's compliance records for six years from the date each was
signed or last in effect, whichever is later.
