# Customer.io email layouts

| Hosted email | Source |
| --- | --- |
| Legacy magic link and account-reset fallback (transactional 2) | [Account link](mlai-account-link.html) |
| Daily research topics (transactional 3) | [Daily topics](../../docs/customerio-daily-topics-email.html) and [plain text](../../docs/customerio-daily-topics-email.txt) |
| Coworking discount reminders (transactional 5 and 6) | [Seven days](../../docs/customerio-monthly-update-reminder-7-day.html) and [one day](../../docs/customerio-monthly-update-reminder-1-day.html) |
| MLAI Chat sign-in code fallback (transactional 7) | [Sign-in code](mlai-chat-sign-in-code.html) |
| Markdown and code-editor marketing or reply-oriented automation | [Campaign layout](mlai-campaign.html) |

Use the code editor for transactional bodies and the Layouts editor for the
campaign shell. The latter inserts each message's existing text at
`{{content}}`. Preserve sender, reply-to, subject, preheader, goal links,
preferences and routing. Review each campaign for a single working unsubscribe
link. For a visual or Design Studio email, add the matching MLAI masthead
in that editor and preserve its existing artwork and controls.

Before a hosted update, export the existing variants for rollback and confirm
the workspace's status, the IDs and their callers. Customer.io's App API
transactional update fully overwrites the variant and cannot update Design
Studio content. In a draft preview, check the synthetic trigger payload,
signed URLs and untracked account links. Preserve the existing workspace
preference footer on the daily topics and discount reminder branches. Preview
before publishing. Updating a template does not need to enrol recipients,
start a workflow or send a campaign.

Victor:AI intake is [retired](../../docs/feature-lifecycle.md). Confirm the
cohort and action URL if the intake is relaunched and transactional 4 is
reused. Transactional 1 is an aggregate of caller-supplied bodies, so apply
branding at the identified caller. Existing template editors and artwork vary
across the investor, BuildDay and eSafety automations. Use the campaign shell
for the code/Markdown bodies, verify the remaining visual editor bodies in the
provider, and keep empty steps and draft automations in their existing states.

[Customer.io layouts](https://docs.customer.io/messaging/channels/email/layouts/)
and [App API](https://docs.customer.io/integrations/api/app/) describe the
supported editor and update paths.
