"""Explicit signed person selections, separate from broadcast/routing p tags."""

import re
from uuid import UUID

from integrations.services.slack_channel_mentions import _mask_code


def native_mention_tags(text, tags):
    """Validate bounded native targets and require their label in source prose.

    Labels only bind a selected key to the signed prose; account identity and
    the returned display name always come from current server records.
    """
    if not isinstance(tags, list):
        raise ValueError("invalid_native_mentions")
    mentions = [tag for tag in tags if isinstance(tag, list) and tag[:1] == ["mlai-mention"]]
    if len(mentions) > 20:
        raise ValueError("invalid_native_mentions")
    result = []
    prose = _mask_code(str(text or ""))
    for tag in mentions:
        if (len(tag) != 3 or not all(isinstance(value, str) for value in tag)
                or not re.fullmatch(r"[0-9a-f]{64}", tag[1])
                or not 1 <= len(tag[2]) <= 80
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "`<>@" for char in tag[2])):
            raise ValueError("invalid_native_mentions")
        pattern = re.compile(
            r"(^|\s|\(|[*_]{1,3}|\|\|)(@" + re.escape(tag[2])
            + r")(?=\|\||[\s,;.!?:)\]}*_]|$)", re.IGNORECASE,
        )
        if pattern.search(prose) and tag not in result:
            result.append(tag)
    return result


def native_mention_bindings(*, workspace_id, text, tags, source_created_at):
    """Snapshot original canonical owners from authenticated source evidence."""
    from .identity import community_account_for_key_at

    bindings = {}
    for _, public_key, _ in native_mention_tags(text, tags):
        account = community_account_for_key_at(
            slack_workspace_id=workspace_id, buzz_pubkey=public_key,
            source_created_at=source_created_at,
        )
        if account is not None:
            bindings[public_key] = {
                "public_key": public_key, "profile_id": account["user_profile_id"],
            }
    return list(bindings.values())


def _binding_profiles(bindings):
    if bindings is None:
        return {}
    if not isinstance(bindings, list) or len(bindings) > 20:
        raise ValueError("invalid_native_mentions")
    result = {}
    for binding in bindings:
        if (not isinstance(binding, dict) or set(binding) != {"public_key", "profile_id"}
                or not isinstance(binding["public_key"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", binding["public_key"])
                or not isinstance(binding["profile_id"], str)):
            raise ValueError("invalid_native_mentions")
        try:
            if str(UUID(binding["profile_id"])) != binding["profile_id"]:
                raise ValueError("invalid_native_mentions")
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid_native_mentions") from exc
        key, profile = binding["public_key"], binding["profile_id"]
        if key in result and result[key] != profile:
            raise ValueError("invalid_native_mentions")
        result[key] = profile
    return result


def native_recipient_targets(*, workspace_id, text, tags, bindings=None):
    """Revalidate current devices against server-pinned original recipients."""
    from .identity import community_account_for_key

    profiles = _binding_profiles(bindings)
    selections, labels = [], {}
    for _, public_key, label in native_mention_tags(text, tags):
        # Original ambiguity cannot disappear when one selected key is later
        # revoked or re-enrolled. Unknown original owners are also ambiguous.
        labels.setdefault(label.casefold(), set()).add(profiles.get(public_key, "unknown:" + public_key))
        if public_key not in profiles:
            continue
        account = community_account_for_key(slack_workspace_id=workspace_id, buzz_pubkey=public_key)
        if account is not None and account["user_profile_id"] == profiles[public_key]:
            profile_id = account["user_profile_id"]
            target = {
                "kind": "mlai_profile", "profile_id": profile_id,
                "display_name": " ".join(str(account["display_name"]).split())[:160],
            }
            slack_id = account.get("slack_user_id")
            if isinstance(slack_id, str) and re.fullmatch(r"[UW][A-Z0-9]+", slack_id):
                target["slack_user_id"] = slack_id
            selections.append((label, profile_id, target))
    # Different devices may share a label and owner. Different canonical
    # people behind indistinguishable labels require a new explicit selection.
    targets = {}
    for label, profile_id, target in selections:
        if len(labels[label.casefold()]) != 1:
            continue
        result = targets.setdefault(profile_id, {**target, "source_labels": []})
        if label not in result["source_labels"]:
            result["source_labels"].append(label)
    return list(targets.values())
