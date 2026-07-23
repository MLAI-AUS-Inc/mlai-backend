DEFAULT_USER_ROLE = "participant"
USER_TEAM_RELATION_NAMES = (
    "hospital_teams",
    "esafety_teams",
    "generic_hackathon_teams",
)


def get_compat_user_role(user=None):
    return DEFAULT_USER_ROLE


def user_has_team(user):
    if not getattr(user, "pk", None):
        return False

    for relation_name in USER_TEAM_RELATION_NAMES:
        relation_manager = getattr(user, relation_name, None)
        if relation_manager is None:
            continue
        if relation_name == "hospital_teams":
            has_team = relation_manager.filter(round__status="active").exists()
        else:
            has_team = relation_manager.exists()
        if has_team:
            return True

    return False
