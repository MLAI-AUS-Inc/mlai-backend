DEFAULT_USER_ROLE = "participant"
USER_TEAM_RELATION_NAMES = (
    "hospital_teams",
    "esafety_teams",
    "innovate_connect_alliance_teams",
)


def get_compat_user_role(user=None):
    return DEFAULT_USER_ROLE


def user_has_team(user):
    if not getattr(user, "pk", None):
        return False

    for relation_name in USER_TEAM_RELATION_NAMES:
        relation_manager = getattr(user, relation_name, None)
        if relation_manager is not None and relation_manager.exists():
            return True

    return False
