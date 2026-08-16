# ADR 002 — SES for user email, not SNS

## Context

Filedrop notifies the uploader by email with a presigned download link. AWS offers three obvious ways to send email from a Lambda:

1. **SNS with an email endpoint.** Cheap, one API call.
2. **SES `SendEmail`.** Transactional email service, full HTML + text, per-recipient sending without pre-registration once out of sandbox.
3. **SMTP via a third party (SendGrid, Postmark, Resend).** Fine but adds a vendor + API key.

At first glance SNS looks attractive: it can send email, and it's the same topic type we're already using for internal fan-out.

## Decision

Use **SES `SendEmail`** from the `notify` Lambda.

The disqualifying property of SNS email is subscription confirmation. Every SNS email endpoint requires the recipient to click a confirmation link on their first message. That is fine for an ops alarm — one person, one confirmation, done — and is exactly why we use SNS for the alarms topic (`filedrop-alarms`) in this project. It is unusable for arbitrary transactional email, because Filedrop has no way to know a user's email address ahead of time or force them to confirm anything before their file is ready.

SES has no such requirement: once the sender identity is verified (and, out of sandbox, once production access is granted), you can send to any address. That matches Filedrop's flow exactly: user submits their email at request time, we email them once.

Third-party SMTP was rejected on the "no external dependencies" scope constraint for this project.

## Consequences

- **Sandbox is a real constraint.** New AWS accounts start with SES in sandbox: recipients must be verified, cap of 200/day. The README documents this and links to the [production-access request](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html). It's a one-time request but not automatic.
- **Sender identity verification is a manual step.** CDK creates the `ses.EmailIdentity` resource, but AWS still emails the sender address a confirmation link. The verify step lives in the README's "manual steps" list.
- **Bounce/complaint handling is not implemented.** SES notifies you of bounces via SNS if configured; we skipped that to stay in scope. If Filedrop ever handled real user email, we'd wire a bounce topic + a suppression list.
- **IAM is tighter than SNS would have been.** The `notify` role's `ses:SendEmail` grant is scoped to the sender identity ARN — the Lambda cannot send from any other identity in the account.
