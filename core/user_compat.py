DEFAULT_USER_ROLE = "participant"
MEDHACK_TEAM_MIN_MEMBERS = 2
MEDHACK_TEAM_MAX_MEMBERS = 6
USER_TEAM_RELATION_NAMES = (
    "hospital_teams",
    "esafety_teams",
    "generic_hackathon_teams",
)


def get_compat_user_role(user=None):
    return DEFAULT_USER_ROLE


def active_hospital_team(user):
    manager = getattr(user, "hospital_teams", None)
    return manager.filter(round__status="active").first() if manager is not None else None


def user_team_profile(user):
    """One compatibility projection for identity reads and profile updates."""
    hospital = active_hospital_team(user)
    esafety_manager = getattr(user, "esafety_teams", None)
    esafety = esafety_manager.first() if esafety_manager is not None else None

    def project(team, *, include_size=False):
        if team is None:
            return None
        members = list(team.members.values("first_name", "last_name", "avatar_url"))
        payload = {
            "team_name": team.team_name,
            "team_id": team.team_id,
            "avatar_url": team.avatar_url,
            "members": [
                {
                    "full_name": f"{member['first_name']} {member['last_name']}".strip(),
                    "avatar_url": member["avatar_url"],
                    "role": DEFAULT_USER_ROLE,
                }
                for member in members
            ],
        }
        if include_size:
            payload["member_count"] = len(members)
            payload["is_valid_team_size"] = (
                MEDHACK_TEAM_MIN_MEMBERS <= len(members) <= MEDHACK_TEAM_MAX_MEMBERS
            )
        return payload

    hospital_data = project(hospital, include_size=True)
    esafety_data = project(esafety)
    generic = getattr(user, "generic_hackathon_teams", None)
    has_team = bool(hospital or esafety)
    if not has_team and generic is not None:
        has_team = generic.exists()
    return {
        "hospital_team": hospital_data,
        "esafety_team": esafety_data,
        "team": hospital_data or esafety_data,
        "has_team": bool(has_team),
    }


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
