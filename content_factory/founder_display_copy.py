"""Plain founder display slots; technical evidence and protocol values stay intact."""
import re
from copy import deepcopy

EXACT = {
    'Scan repository': 'Check website',
    'Scan the current repository.': 'Check your latest website changes.',
    'Select and verify your website repository.': 'Choose your website code on GitHub.',
    'Prepare and verify your articles integration.': 'Connect your blog so MLAI can add articles.',
    'Your articles setup pull request is ready. Review the changes, then publish.': 'Your blog page is ready. Review the changes, then add it to your website.',
    'Return to your setup pull request': 'Return to your blog page',
    'Build proof only; articles stay portable until a publishing adapter is connected.': 'Your draft is ready to copy. Connect your blog to add it to your website.',
    'Check saved GitHub access.': 'Check GitHub access.',
    "Your website's publishing verification is out of date.": 'We need to check your latest website changes.',
    'Verify the deployed public articles route.': 'Check your live blog address.',
    'Website repository': 'Website code on GitHub',
    'Repository inventory': 'Website check',
    'GitHub workflow permissions': 'GitHub access',
    'Articles integration': 'Blog connection',
    'Build and render verification': 'Website preview',
    'Live articles route': 'Live blog address',
    'Recheck verification': 'Check website again',
    'Verify website': 'Check website',
    'Custom contract': 'Website connection details',
    'Ci attestation': 'Website check details',
    'Reconcile': 'Check progress',
    'Cancel operation': 'Stop for now',
    'Verify cleanup': 'Check removed changes',
}
TECHNICAL = re.compile(r'\b(?:scaffold|repository|repos?|inventory|scans?|adapters?|integration|portable|pull request|PR|deploy(?:ed|ment)?|CI|workflows?|attestation|verif(?:y|ied|ication)|operations?|receipts?|workers?)\b', re.I)


def plain_founder_journey(value):
    """Translate only visible labels/reasons, leaving all authority and support evidence unchanged."""
    result = deepcopy(value)
    def text(value):
        if not isinstance(value, str):
            return value
        return EXACT.get(value, 'Check your website settings to continue.' if TECHNICAL.search(value) else value)
    for key in ('reason',):
        result[key] = text(result.get(key))
    policy = result.get('policy')
    if isinstance(policy, dict):
        policy['reason'] = text(policy.get('reason'))
    for item in result.get('prerequisites', {}).values():
        if isinstance(item, dict):
            item['reason'] = text(item.get('reason'))
    for item in [*result.get('steps', []), *result.get('actions', [])]:
        if isinstance(item, dict):
            for key in ('label', 'reason', 'user_message'):
                if key in item:
                    item[key] = text(item[key])
    action = result.get('nextAction')
    if isinstance(action, dict):
        action['label'], action['reason'] = text(action.get('label')), text(action.get('reason'))
    return result
